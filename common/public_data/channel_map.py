"""渠道映射表 ``dim_channel_map``（渠道管线迁 WDT 第 2 步，2026-10-23）。

背景：渠道日销此前唯一真源是 AI 表人工台账（每渠道一个 sheet），
存在预置空行、停填、未来日期等病灶（见 extract_mart 的预置行过滤）。
WDT 订单自 PR #223 起带 ``buyer_nick``（渠道抬头），可以按抬头自动取数。

本模块只做**一件事**：把一条订单行（shop_name / buyer_nick）解析成渠道键。
聚合与管道是第 3 步，不在本模块。

口径裁决（2026-10-23 运维）：
1. 丰享 / 海澜之家 / 酱云汇 等 AI 表无 sheet 的 B2B 客户——**单独设计一个
   面板**（panel=channel_b2b），不并入现有七大渠道面板。
2. 黄贤宋同时是私域与海康威视的抬头，订单不可分——合并算**私域（含海康）**。
3. 即时零售 sheet 里的「餐饮」「其它」暂无确定抬头——先映射到兜底渠道
   **其它**（panel=channel_b2b），后续拿到抬头再拆。

匹配真值来自云上 probe（fact_order_line 38 个抬头分组，2026-08~10）：
抬头写法不统一（同一主体混用全角/半角括号，如「浙江天猫网络科技有限公司
（寄售）」与「(经销）」），故匹配前统一归一化（见 normalize_match_value）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_CHANNEL_MAP_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_channel_map` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `channel_key` VARCHAR(32) NOT NULL,\n"
    "  `channel_label` VARCHAR(64) NOT NULL,\n"
    "  `panel` VARCHAR(32) NOT NULL DEFAULT 'channel',\n"
    "  `business_line` VARCHAR(32) NOT NULL DEFAULT 'online',\n"
    "  `match_type` VARCHAR(16) NOT NULL,\n"
    "  `match_mode` VARCHAR(8) NOT NULL DEFAULT 'exact',\n"
    "  `match_value` VARCHAR(255) NOT NULL,\n"
    "  `priority` INT NOT NULL DEFAULT 100,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_channel_map` (`match_type`, `match_value`),\n"
    "  KEY `idx_channel_enabled` (`channel_key`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_channel_map_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `channel_key` VARCHAR(32) DEFAULT NULL,\n"
    "  `match_type` VARCHAR(16) DEFAULT NULL,\n"
    "  `match_value` VARCHAR(255) DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 读面列序（fetch_channel_map 的 SELECT 与之逐位一致）。
_MAP_KEYS = (
    "id", "channel_key", "channel_label", "panel", "business_line",
    "match_type", "match_mode", "match_value", "priority", "note",
    "enabled",
)

#: 匹配类型取值（对齐订单行可用字段；shop 预留给按店铺区分的 B2C 平台）。
MATCH_SHOP = "shop"
MATCH_BUYER_NICK = "buyer_nick"

#: 面板取值：现有七大渠道面板 / B2B 客户新面板（2026-10-23 裁决）。
PANEL_CHANNEL = "channel"
PANEL_B2B = "channel_b2b"

#: 业务线分档（2026-10-23 裁决 5）：线上渠道 / 线下网点 / 餐饮 / 体验馆与团购。
#: 与 panel（展示面板）正交——panel 决定「放哪块面板」，business_line 决定
#: 「算哪条业务线」。
BL_ONLINE = "online"
BL_OFFLINE_STORE = "offline_store"
BL_DINING = "dining"
BL_EXPERIENCE = "experience_group"

BUSINESS_LINES = (
    (BL_ONLINE, "线上渠道"),
    (BL_OFFLINE_STORE, "线下网点"),
    (BL_DINING, "餐饮"),
    (BL_EXPERIENCE, "体验馆与团购"),
)

#: 区域键 → 业务线（区域侧口径，订单走 buyer_nick/shop 映射，两者在
#: business_line 上对齐）。vanke = 万科/莲荷里体验馆 + 企业团购（2026-10-23
#: 裁决）；餐饮部门独立成线；其余线下区域为线下网点。
REGION_BUSINESS_LINE = {
    "hangzhou": BL_OFFLINE_STORE,
    "shaoxing": BL_OFFLINE_STORE,
    "offline_all": BL_OFFLINE_STORE,
    "vanke": BL_EXPERIENCE,
    "dining": BL_DINING,
    "qudao": BL_ONLINE,
}

_SELECT_SQL = (
    "SELECT `id`, `channel_key`, `channel_label`, `panel`, `business_line`, "
    "`match_type`, `match_mode`, `match_value`, `priority`, `note`, `enabled` "
    "FROM `dim_channel_map` {where} ORDER BY `priority` ASC, `id` ASC"
)

#: 已建库补齐 business_line 列（v1 先建表后加列的存量库）。
_CHANNEL_MAP_V2_DDL = (
    "ALTER TABLE `dim_channel_map` "
    "ADD COLUMN `business_line` VARCHAR(32) NOT NULL DEFAULT 'online' "
    "AFTER `panel`"
)


def channel_map_ddl_statements() -> tuple:
    """迁移语句：映射表 + 审计表（建表顺序即返回顺序）。"""
    return (_CHANNEL_MAP_DDL, _AUDIT_DDL)


def channel_map_v2_ddl_statements() -> tuple:
    """v2：业务线分类列（2026-10-23 裁决 5）——线上/线下/餐饮/体验馆与团购。

    独立一条迁移而非改 v1：v1 已在生产执行过（CREATE TABLE IF NOT EXISTS
    不会补列），存量库只能靠 ALTER 追平。
    """
    return (_CHANNEL_MAP_V2_DDL,)


# ---------------------------------------------------------------------------
# 归一化与解析
# ---------------------------------------------------------------------------

#: 全角括号/空格/常见分隔符 → 统一形态（云上实测同一主体混用全角半角）。
_NORMALIZE_TABLE = str.maketrans({
    "（": "(", "）": ")", " ": "", "\u3000": "", " ": "",
})
_SPACE_RE = re.compile(r"\s+")


def normalize_match_value(value) -> str:
    """匹配值归一化：去空白、全角括号转半角、小写。

    云上实锤：``浙江天猫网络科技有限公司（寄售）`` 与
    ``浙江天猫网络科技有限公司(经销）`` 括号全半角混写，不归一化会漏匹配。
    """
    text = "" if value is None else str(value)
    text = _SPACE_RE.sub("", text).translate(_NORMALIZE_TABLE)
    return text.lower()


def fetch_channel_map(connection, enabled_only: bool = True) -> list[dict]:
    """读取映射规则（按 priority 升序；表不存在时 fail-soft 返回空列表）。

    表未迁移（或迁移失败）时返回空——调用方按「无映射」处理，不阻断管道。
    """
    where = "WHERE `enabled` = 1" if enabled_only else ""
    try:
        with connection.cursor() as cur:
            cur.execute(_SELECT_SQL.format(where=where))
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("dim_channel_map 读取失败（%s），按无映射处理",
                       type(exc).__name__)
        return []
    return [dict(zip(_MAP_KEYS, row if isinstance(row, (tuple, list))
                     else [row[k] for k in _MAP_KEYS])) for row in rows]


def resolve_channel(row, rules) -> str | None:
    """订单行 → 渠道键；无匹配返回 ``None``（调用方决定是否兜底）。

    ``row`` 需含 ``shop_name`` / ``buyer_nick`` 键（缺键按空处理）。
    规则按 priority 升序取首个命中：exact 全等、prefix 前缀。
    """
    if not rules:
        return None
    shop = normalize_match_value(row.get("shop_name"))
    nick = normalize_match_value(row.get("buyer_nick"))
    # 规则按 priority 升序取首个命中（SQL 已排序，此处再排一次防御调用方
    # 传入未排序的集合——排序对幂等结果至关重要，同值取先定义者）。
    ordered = sorted(rules, key=lambda r: int(r.get("priority") or 0))
    for rule in ordered:
        source = shop if rule.get("match_type") == MATCH_SHOP else nick
        if not source:
            continue
        target = normalize_match_value(rule.get("match_value"))
        if not target:
            continue
        mode = rule.get("match_mode")
        if mode == "prefix":
            if source.startswith(target):
                return rule["channel_key"]
        elif mode == "contains":
            # 店铺名形如「习水村-抖音习酒酒类旗舰店」：平台词嵌在主体前缀之后，
            # 只能按包含判定（拼多多/抖音/视频号/快手/京东皆如此，2026-10-23）。
            if target and target in source:
                return rule["channel_key"]
        elif source == target:
            return rule["channel_key"]
    return None


# ---------------------------------------------------------------------------
# 默认映射（云上 probe 真值，2026-10-23）
# ---------------------------------------------------------------------------

#: ``(channel_key, channel_label, panel, business_line, match_type,
#:   match_mode, match_value, priority, note)``——写入侧按
#: (match_type, match_value) 幂等 upsert。
#:
#: 匹配优先级设计：B2B 抬头（buyer_nick exact，priority 10-40）先判，平台
#: 店铺（shop contains，70+）兜底。B2C 单 buyer_nick 恒空，只能靠店铺名里的
#: 平台词区分（云上实锤：习水村-抖音…/杭易-拼多多…/习水村-视频号…）。
DEFAULT_MAP_ROWS = (
    # --- 主面板：现有七大渠道（对齐 CHANNEL_ORDER 的中文键） ---
    ("猫超", "猫超", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "浙江天猫网络科技有限公司(寄售)", 10,
     "WDT 最大 B2B 抬头（19,264 单 / 1,866 万，08-24~10-22）"),
    ("猫超", "猫超", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "浙江天猫网络科技有限公司(经销)", 11,
     "1,077 单 / 702 万；2026-10-23 裁决：猫超 = 寄售 + 经销"),
    ("京东", "京东", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "北京京东世纪信息技术有限公司", 12,
     "44 单 / 914 万（B2B 大额，客单极高）"),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "福州朴朴电子商务有限公司", 20, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "厦门朴朴电子商务有限公司", 21, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "深圳市朴朴网络科技有限公司", 22, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "广州市朴朴网络科技有限公司", 23, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "武汉朴朴网络科技有限公司", 24, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "成都朴朴电子商务有限公司", 25, None),
    ("即时零售", "即时零售", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "多多买菜", 26, "6 单 / 10.6 万（10-08~10-09）"),
    ("私域", "私域（含海康）", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "私域零售", 30, "131 单 / 16 万"),
    ("私域", "私域（含海康）", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "黄贤宋", 31, "海康威视与私域同一抬头不可分，2026-10-23 裁决合并"),
    ("直播", "直播", PANEL_CHANNEL, BL_ONLINE, MATCH_BUYER_NICK, "prefix",
     "直播间寄样", 40, "寄样（待定/赠送）归直播，口径待核对"),
    # --- B2B 新面板（2026-10-23 裁决：单独设计一个面板） ---
    ("丰享", "丰享", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "深圳丰享信息技术有限公司", 50, "262 单 / 10 万"),
    ("海澜之家", "海澜之家", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "江阴澜韵实业有限公司", 51, "4 单（小额 B2B）"),
    ("酱云汇", "酱云汇", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "酱韵科技(上海)有限责任公司", 52, "2 单（小额 B2B）"),
    # --- 兜底「其它」：餐饮客户独立成餐饮业务线（裁决 5） ---
    ("其它", "其它", PANEL_B2B, BL_DINING, MATCH_BUYER_NICK, "exact",
     "杭州苏丽珍餐饮管理有限公司", 60,
     "即时零售 sheet 的「餐饮」候选（51 行 / 71 万），2026-10-23 归餐饮"),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "苏州鲁智深商贸有限公司", 61, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "浙江易元宏雷供应链管理有限公司", 62, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "杭州易元宏雷贸易有限公司", 63, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "绍兴上虞佳华酒业有限公司", 64, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "遵义贵世贸易有限公司", 65, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "诸暨好辰酒业有限公司", 66, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "绍兴市上虞区曹娥街道百良副食品店", 67, None),
    ("其它", "其它", PANEL_B2B, BL_ONLINE, MATCH_BUYER_NICK, "exact",
     "久一点吧小卖部", 68, None),
    # --- 平台店铺规则（B2C 单 buyer_nick 恒空，只能按店铺名区分）——
    # 2026-10-23 裁决 2：拼多多/抖音按店铺名映射区分。云上店铺命名实锤
    # 「主体-平台+店名」（习水村-抖音习酒酒类旗舰店 / 杭易-拼多多致中和… /
    # 习水村-视频号… / 习水村-快手… / 杭易-京东购喝旗舰店sop）。
    ("直播", "直播（抖音+视频号）", PANEL_CHANNEL, BL_ONLINE, MATCH_SHOP,
     "contains", "抖音", 70, "AI 表「直播」= 抖音 + 视频号"),
    ("直播", "直播（抖音+视频号）", PANEL_CHANNEL, BL_ONLINE, MATCH_SHOP,
     "contains", "视频号", 71, "同上，微信视频号归直播"),
    ("拼多多", "拼多多", PANEL_CHANNEL, BL_ONLINE, MATCH_SHOP, "contains",
     "拼多多", 72, "B2C 零售单，按店铺名区分平台"),
    ("京东", "京东", PANEL_CHANNEL, BL_ONLINE, MATCH_SHOP, "contains",
     "京东", 73, "京东 POP 店铺（与 B2B 抬头同渠道键）"),
    ("其它", "其它（新兴平台）", PANEL_B2B, BL_ONLINE, MATCH_SHOP, "contains",
     "快手", 74, "AI 表无对应 sheet，暂归其它"),
    ("其它", "其它（新兴平台）", PANEL_B2B, BL_ONLINE, MATCH_SHOP, "contains",
     "得物", 75, "AI 表无对应 sheet，暂归其它"),
    ("其它", "其它（新兴平台）", PANEL_B2B, BL_ONLINE, MATCH_SHOP, "contains",
     "微盟", 76, "AI 表无对应 sheet，暂归其它"),
)

#: 已全部可映射（拼多多/抖音经店铺名规则解决，2026-10-23）。保留常量供
#: 第 3 步聚合自检：出现未映射渠道键时说明映射表落后于新开店铺。
UNMAPPED_CHANNELS: tuple = ()
