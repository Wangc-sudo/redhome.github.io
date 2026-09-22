# -*- coding: utf-8 -*-
"""Stream 报数落库（spec §9：报数 → gateway → 直接落 ``mart_ops``）。

``listener.ReportHandler`` 的 mart 版：门禁、解析、回执文案与现行逐字
一致，差别只在**落点与身份来源**：

* 身份来自 ``dim_robot_member``（``sender_staff_id`` 直查），不再维护
  「姓名 ↔ userId」白名单文件；
* 数值直写 ``fact_daily_report_offline``，不再回写钉钉 AI 表（其降级为
  非真源）；
* 月累计/完成率走共享口径 :mod:`common.metrics.daily_report`（含当天），
  不再就地求和。

**写入规则（业务键优先）**：按 ``(region, 表内用名, business_date)`` 查找
既有事实行——存在则就地更新 ``sales_amount``（保留其目标列与 PK），不
存在才插入 ``stream:{region}:{user_id}:{date}`` 主键的新行。这样切换
窗口内不会产生同人同日的重复行；切换完成后日报的 AI 表同步退役，
extract 不再重放这些行，冲突面整体消失。

**回执错误文案的有意改动**：处理异常的回执不再插值异常原文（现行
``f"⚠️ 处理报数时出错：{e}"`` 可能泄露内部细节），改为通用提示。
"""

import contextlib
import re
from dataclasses import dataclass
from datetime import date, datetime

from common.daily_robot.mart_tasks import MartTaskError
from common.metrics.daily_report import (
    elapsed_workdays,
    fetch_filled_names,
    fetch_member,
    fetch_month_facts,
    fetch_region_members,
    fetch_workdays,
    summarize_people,
    unfilled_members,
)


_WEEKDAYS = "一二三四五六日"

#: stream 来源写入的占位 run id（全零）：报数落库不属于任何同步 run，
#: 用它与 sync/extract 产生的行区分，便于审计与排查。
STREAM_RUN_ID = "00000000-0000-0000-0000-000000000000"

_AMOUNT_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")

#: 报数指标词（多板块报数只认这两个，2026-09-22 与运维定稿）。
_METRIC_WORDS = ("零售", "团购")

#: 门店别名（过渡期）：规范名 = 通讯录部门名（板块名与组织架构绑定）；
#: 群里的口语写法映射到规范名，填报习惯统一后可移除。
_STORE_ALIASES = {
    "万科体验馆": "万科体验馆",
    "万科": "万科体验馆",
    "莲荷里体验馆": "莲荷里体验馆",
    "莲荷里": "莲荷里体验馆",
    "酱酒体验馆": "莲荷里体验馆",
    "酱香体验馆": "莲荷里体验馆",
    "酱酒": "莲荷里体验馆",
    "酱香": "莲荷里体验馆",
    "大莲花": "莲荷里体验馆",
}

#: 规范门店名（去重保序）。
_CANONICAL_STORES = tuple(dict.fromkeys(_STORE_ALIASES.values()))

#: 多板块报数分词：门店词 / 指标词 / 数字 / 其他中文字，按序扫描
#: （门店词长词优先；单字指标「团」「零」仅在后随数字/空白/逗号时成立，
#: 防「零食」「团队」之类误判；未命中门店的词进入 word 组做模糊补全）。
_TOKEN_RE = re.compile(
    r"(?P<store>" + "|".join(sorted(_STORE_ALIASES, key=len, reverse=True)) + r")"
    r"|(?P<metric>零售|团购|团(?=[\d\s,])|零(?=[\d\s,]))"
    r"|(?P<num>-?\d[\d,]*(?:\.\d+)?)"
    r"|(?P<word>[一-鿿]+)"
)


def _resolve_store_fuzzy(word):
    """门店词模糊补全（自动补全 A）：与规范名/别名前缀互含且候选唯一才认。

    返回 ``(规范名, 原词)``；候选不唯一或无命中、词长 <2 → ``None``。
    """
    if not word or len(word) < 2:
        return None
    targets = {
        _STORE_ALIASES.get(candidate, candidate)
        for candidate in tuple(_STORE_ALIASES) + _CANONICAL_STORES
        if candidate.startswith(word) or word.startswith(candidate)
    }
    if len(targets) != 1:
        return None
    canonical = next(iter(targets))
    if canonical == word:
        return None
    return canonical, word


@dataclass(frozen=True)
class IntakeOutcome:
    """一条报数消息的处理结果（reply 为给发送者的回执文本）。"""

    status: str  # recorded | not_workday | not_member | no_number | aux
    reply: str
    region: str | None = None
    name: str | None = None
    value: object = None
    overwritten: bool = False


# ---------------------------------------------------------------------------
# 解析与回执文案（与 listener.py 逐字一致）
# ---------------------------------------------------------------------------

def parse_report_amount(text):
    """从报数文本提取金额；无法识别返回 ``None``。

    规则与现行 listener 一致：全角逗号归一、千分位逗号剔除、取文本中
    第一个数字（允许负数与小数），整数值返回 ``int``。
    """
    match = _AMOUNT_RE.search((text or "").replace("，", ","))
    if not match:
        return None
    num_str = match.group(1).replace(",", "")
    try:
        value = float(num_str)
    except ValueError:
        return None
    return int(value) if value == int(value) else value


def parse_report_metrics(text):
    """多板块报数解析：``[(门店规范名|None, 指标, 金额, 识别原词|None), ...]``。

    规则（2026-09-22 与运维定稿；2026-09-23 增补自动补全）：
    - 指标认「零售」「团购」，单字「团」「零」仅在后随数字/空白/逗号时容错；
    - 门店词出现后成为后续数字的归属上下文，直到下一个门店词；
    - 门店词后直接跟数字 → 该店「零售」（如 ``万科体验馆/7560``）；
    - 不在别名表的中性词做模糊补全（前缀互含+候选唯一），命中时第 4 元素
      记录原词用于回执回显（如 ``莲荷`` → ``莲荷里体验馆``）；
    - 无门店词 → 门店为 ``None``（由调用方按报数人部门补齐）；
    - 全文不含任何门店/指标词 → 返回 ``None``（走单金额旧路径）。
    """
    if not text:
        return None
    entries = []
    saw_label = False
    ctx_store = None
    ctx_recognized = None
    pending_store = False
    pending_metric = None
    for m in _TOKEN_RE.finditer(text.replace("，", ",")):
        if m.group("store"):
            ctx_store = _STORE_ALIASES[m.group("store")]
            ctx_recognized = None
            pending_store = True
            pending_metric = None
            saw_label = True
        elif m.group("metric"):
            token = m.group("metric")
            pending_metric = {"团": "团购", "零": "零售"}.get(token, token)
            pending_store = False
            saw_label = True
        elif m.group("word"):
            resolved = _resolve_store_fuzzy(m.group("word"))
            if resolved is not None:
                ctx_store, ctx_recognized = resolved
                pending_store = True
                pending_metric = None
                saw_label = True
        else:
            num_str = m.group("num").replace(",", "")
            try:
                value = float(num_str)
            except ValueError:
                continue
            value = int(value) if value == int(value) else value
            metric = pending_metric or "零售"
            entries.append((ctx_store, metric, value, ctx_recognized))
            pending_metric = None
            pending_store = False
    if not saw_label or not entries:
        return None
    return entries


def build_store_denied_reply(name, sender_dept, store):
    return (
        f"⛔ {name} 你好，你的部门是{sender_dept}，不能报{store}的数据哦。\n"
        "如有疑问请联系管理员。"
    )


def build_multi_recorded_reply(*, month, day, weekday, writes):
    """多板块回执。writes = ``[(数据格, 金额, 旧值|None, progress|None, 识别备注|None), ...]``
    （兼容 4 元组：无识别备注）。"""
    lines = [f"✅ 已记录 {month}月{day}日（周{weekday}）："]
    for write in writes:
        cell, value, old_value, progress = write[:4]
        note = write[4] if len(write) > 4 else None
        line = f"{cell}：{value}"
        if note:
            line += note
        if old_value is not None:
            line += f"（🔁 覆盖旧值 {old_value}）"
        lines.append(line)
        if progress is not None:
            total_disp, target_disp, ratio_str = progress
            lines.append(f"📊 {cell} 本月累计 {total_disp} / 目标 {target_disp}，完成 {ratio_str}")
    lines.append("祝您下班愉快 🎉")
    return "\n".join(lines)


def build_not_member_reply():
    return (
        "⛔ 报数功能仅限销售日报责任人使用。\n"
        "如需填写日报请联系管理员，或在表格中直接填写。"
    )


def build_not_workday_reply():
    return "今天不是销售日报工作日，无需报数～"


def build_format_hint(name, store=None, stores=()):
    """格式提示（自动补全 B）：多门店区域按发送人部门个性化——本店成员
    给本店示例，根部门成员给门店列表示例；其他区域保持通用文案。
    统一附 /辅助指令 入口（2026-09-23 运维要求提示跟上指令时代）。"""
    lines = [
        f"{name} 你好～报数格式：@提醒事项 数字",
        "例如：@提醒事项 12800（当天无销量报 0）",
        "查看全部功能：/帮助 ｜ 按钮菜单：/菜单",
    ]
    if store:
        lines.append(
            f"你的门店是{store}，也可以这样报：{store} 零售 7560，"
            f"或 {store} 团购 1200"
        )
    elif stores:
        lines.append(
            "多门店报数示例：" + "；".join(f"{s} 零售 7560" for s in stores)
        )
    return "\n".join(lines)


def build_error_reply():
    return "⚠️ 处理报数时出错，请稍后再试，或直接在表格中填写。"


def build_recorded_reply(*, month, day, weekday, value, old_value, progress):
    """✅ 回执。old_value 为已格式化的旧值（或 None）；progress 为
    ``(total_disp, target_disp, ratio_str)`` 或 None。"""
    lines = [f"✅ 已记录 {month}月{day}日（周{weekday}）销量：{value}"]
    if old_value is not None:
        lines.append(f"🔁 已覆盖你之前填报的 {old_value}")
    if progress is not None:
        total_disp, target_disp, ratio_str = progress
        lines.append(f"📊 本月累计 {total_disp} / 目标 {target_disp}，完成 {ratio_str}")
    lines.append("祝您下班愉快 🎉")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 按群路由（spec §9：单连接、conversationId → region）
# ---------------------------------------------------------------------------

def region_for_conversation(region_configs, conversation_id):
    """按 ``open_conversation_id`` 反查区域配置；未登记返回 ``None``。"""
    if not conversation_id:
        return None
    for cfg in region_configs.values():
        if cfg.open_conversation_id == conversation_id:
            return cfg
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# “/” 辅助指令（2026-09-23 运维定稿）：查询类门禁同报数；
# /补签 仅 bi_authz_grant admin（管理人代录通道，最长追溯 30 天）。
# ---------------------------------------------------------------------------

#: 补签可追溯天数上限。
_AUX_MAX_BACKFILL_DAYS = 30

#: /补签 参数：姓名 [日期] 正文。日期支持 9-20 / 9/20 / 9月20(日) / 0920，
#: 段间须空白/逗号/冒号分隔（防「12800」被误切出日期段）。
_AUX_BACKFILL_RE = re.compile(
    r"^(?P<name>[一-鿿A-Za-z]{2,})[\s,，:：]+"
    r"(?:(?P<day_token>(?:\d{1,2}\s*[-/月]\s*\d{1,2}日?)|(?:\d{4}))[\s,，:：]+)?"
    r"(?P<body>\S.*)$"
)


def parse_aux_command(text):
    """``/`` 辅助指令解析。返回 ``(指令, 参数)``；非 ``/`` 开头 → ``None``。

    未识别的 ``/`` 指令一律落到帮助菜单——不放进报数路径，防误录。
    """
    if not text:
        return None
    body = text.strip()
    # 钉钉群 @机器人 的消息，投递文本可能保留 @前缀（@提醒事项 /帮助）；
    # 部分客户端还会插入零宽字符，统一剥离防匹配失效。
    body = re.sub("[​‌‍⁠﻿]", "", body)
    body = re.sub(r"^@[^\s/]+\s*", "", body)
    # 第 1 行字符类为零宽字符：U+200B U+200C U+200D U+2060 U+FEFF；
    # 第 2 行 @昵称 在空白或 / 前停住（兼容 @机器人/菜单 连写）。
    if not body.startswith("/"):
        return None
    body = body[1:].strip()
    if body in ("未填", "我的", "门店"):
        return (body, None)
    if body.startswith("补签"):
        return ("补签", _AUX_BACKFILL_RE.match(body[2:].strip()))
    return ("帮助", None)


def _parse_backfill_date(token, today):
    """补签日期：缺省=今天；年份取不晚于今天的最近一次；未来/超 30 天 → None。"""
    if not token:
        return today
    token = token.replace(" ", "")
    matched = re.fullmatch(r"(\d{1,2})[-/月](\d{1,2})日?", token)
    if matched:
        month, day = int(matched.group(1)), int(matched.group(2))
    elif re.fullmatch(r"\d{4}", token):
        month, day = int(token[:2]), int(token[2:])
    else:
        return None
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate <= today:
            break
    else:
        return None
    if (today - candidate).days > _AUX_MAX_BACKFILL_DAYS:
        return None
    return candidate


def build_aux_help(name, store=None, stores=()):
    """辅助指令菜单 + 按发送人部门个性化的报数示例（复用 B 项口径）。"""
    lines = [
        "🤖 辅助指令：",
        "/帮助 — 本菜单",
        "/未填 — 今日未填名单",
        "/我的 — 我的本月进度（累计/目标/完成率）",
        "/门店 — 各门店本月进度（多门店区域）",
        "/补签 姓名 [日期] 金额 — 管理人代录（最长 30 天，如 /补签 张三 9-20 12800）",
        "",
        build_format_hint(name, store=store, stores=stores),
    ]
    return "\n".join(lines)


def _build_unfilled_reply(*, month, day, names=None, cells=None):
    if names is not None:
        if not names:
            return f"🎉 {month}月{day}日全部已填报，辛苦了！"
        return (
            f"📋 截至现在，{month}月{day}日还有 {len(names)} 位未填报：\n"
            + "、".join(names)
        )
    if not cells:
        return f"🎉 {month}月{day}日各门店板块全部已报，辛苦了！"
    return (
        f"📋 截至现在，{month}月{day}日未报板块：\n" + "、".join(cells)
    )


def _progress_line(label, progress):
    if progress is None:
        return None
    total_disp, target_disp, ratio_str = progress
    return f"📊 {label} 本月累计 {total_disp} / 目标 {target_disp}，完成 {ratio_str}"


def _aux_unfilled(connection, *, region_cfg, business_date):
    """今日未填：多门店区域按板块格口径，其余区域按成员口径（同 18:30 提醒）。"""
    members = fetch_region_members(connection, region=region_cfg.region)
    filled = fetch_filled_names(
        connection, region=region_cfg.region, business_date=business_date
    )
    member_depts = {m.get("dept_name") for m in members}
    stores = [s for s in _CANONICAL_STORES if s in member_depts]
    if stores:
        expected = [f"{s}·{metric}" for s in stores for metric in _METRIC_WORDS]
        missing = [cell for cell in expected if cell not in filled]
        return _build_unfilled_reply(
            month=business_date.month, day=business_date.day, cells=missing
        )
    names = [
        m["name"]
        for m in unfilled_members(members, filled, aliases=region_cfg.aliases)
    ]
    return _build_unfilled_reply(
        month=business_date.month, day=business_date.day, names=names
    )


def _aux_mine(connection, *, region_cfg, name, sender_dept,
              business_date, workdays):
    """我的本月进度：多门店区域给名下两格，其余区域给本人表内用名一格。"""
    if sender_dept in _CANONICAL_STORES:
        lines = []
        for metric in _METRIC_WORDS:
            cell = f"{sender_dept}·{metric}"
            line = _progress_line(cell, _progress(
                connection, region=region_cfg.region, table_name=cell,
                business_date=business_date, workdays=workdays,
            ))
            lines.append(line or f"{cell}：本月暂无数据或无目标")
        return "\n".join([f"📊 {name} 本月进度："] + lines)
    table_name = region_cfg.aliases.get(name, name)
    line = _progress_line(table_name, _progress(
        connection, region=region_cfg.region, table_name=table_name,
        business_date=business_date, workdays=workdays,
    ))
    if line is None:
        return f"📊 {name} 本月暂无数据或无目标（表内用名：{table_name}）"
    return f"{line}（含今天）"


def _aux_stores(connection, *, region_cfg, business_date, workdays,
                all_region_cfgs=None, is_admin=False):
    """各门店本月进度（只列有有效目标的格；非多门店区域明确提示）。

    管理员覆盖（2026-09-23 运维定稿）：本区域无多门店数据且发送人是
    admin 时，跨区列出首个有多门店数据的区域（标注管理员视图）。
    """
    def _lines_for(region_name):
        lines = []
        for store in _CANONICAL_STORES:
            for metric in _METRIC_WORDS:
                cell = f"{store}·{metric}"
                line = _progress_line(cell, _progress(
                    connection, region=region_name, table_name=cell,
                    business_date=business_date, workdays=workdays,
                ))
                if line:
                    lines.append(line)
        return lines

    lines = _lines_for(region_cfg.region)
    if lines:
        return "\n".join(lines)
    if is_admin and all_region_cfgs:
        for cfg in all_region_cfgs:
            if cfg.region == region_cfg.region:
                continue
            lines = _lines_for(cfg.region)
            if lines:
                return "\n".join(
                    [f"📊 {cfg.display} 各店进度（管理员跨区视图）："] + lines
                )
    return "本区域暂无多门店板块数据（可用 /我的 查个人进度）。"


def _record_value(connection, *, region_cfg, member, table_name,
                  business_date, value, now, workdays, key_suffix=None):
    """单格写入 + 进度（/补签 复用）。返回 ``(数据格, 金额, 旧值文案|None, progress)``。"""
    old_value = _write_report(
        connection,
        region=region_cfg.region,
        member=member,
        table_name=table_name,
        monthly_target=region_cfg.monthly_targets.get(table_name),
        business_date=business_date,
        value=value,
        now=now,
        key_suffix=key_suffix,
    )
    progress = _progress(
        connection, region=region_cfg.region, table_name=table_name,
        business_date=business_date, workdays=workdays,
    )
    overwritten = old_value is not None and float(old_value) != float(value)
    return (
        table_name,
        value,
        _fmt_amount(old_value) if overwritten else None,
        progress,
    )


def _aux_backfill(connection, match, *, region_cfg, sender_uid, name, now):
    """管理人代录：/补签 姓名 [日期] 金额（或含门店/指标词的报数正文）。

    署名为目标成员（业务键与本人自报一致，重报走覆盖）；回执注明代录人。
    """
    if not _is_admin(connection, sender_uid):
        return IntakeOutcome(
            "not_member",
            "⛔ /补签 仅限管理人使用（需在 bi_authz_grant 持 admin 授权）。",
            region=region_cfg.region, name=name,
        )
    if match is None:
        return IntakeOutcome(
            "aux",
            "补签格式：/补签 姓名 [日期] 金额\n"
            "例如：/补签 张三 12800（今天），/补签 张三 9-20 12800（指定日期）",
            region=region_cfg.region, name=name,
        )
    today = now.date() if isinstance(now, datetime) else now
    business_date = _parse_backfill_date(match.group("day_token"), today)
    if business_date is None:
        return IntakeOutcome(
            "aux",
            f"⛔ 日期超出范围：仅支持今天起 {_AUX_MAX_BACKFILL_DAYS} 天内，"
            "格式 9-20 / 9/20 / 9月20 / 0920。",
            region=region_cfg.region, name=name,
        )
    target_name = match.group("name")
    members = fetch_region_members(connection, region=region_cfg.region)
    targets = [m for m in members if m["name"] == target_name]
    if len(targets) != 1:
        return IntakeOutcome(
            "aux",
            f"⛔ 未找到成员「{target_name}」（本区域在册实名），请核对姓名。",
            region=region_cfg.region, name=name,
        )
    target = targets[0]
    workdays = fetch_workdays(
        connection, year=business_date.year, month=business_date.month
    )
    if business_date not in workdays:
        return IntakeOutcome(
            "aux",
            f"{business_date.month}月{business_date.day}日不是工作日，无需补签～",
            region=region_cfg.region, name=name,
        )
    body = match.group("body")
    writes = []
    entries = parse_report_metrics(body)
    if entries is not None:
        target_dept = target.get("dept_name")
        for store_label, metric, value, _recognized in entries:
            store = store_label or target_dept
            if not store:
                return IntakeOutcome(
                    "aux", f"⛔ 无法确定「{target_name}」的门店归属。",
                    region=region_cfg.region, name=name,
                )
            writes.append(_record_value(
                connection, region_cfg=region_cfg, member=target,
                table_name=f"{store}·{metric}", business_date=business_date,
                value=value, now=now, workdays=workdays, key_suffix=metric,
            ))
    else:
        value = parse_report_amount(body)
        if value is None:
            return IntakeOutcome(
                "aux", "⛔ 补签金额未识别（正文须含数字）。",
                region=region_cfg.region, name=name,
            )
        table_name = region_cfg.aliases.get(target["name"], target["name"])
        writes.append(_record_value(
            connection, region_cfg=region_cfg, member=target,
            table_name=table_name, business_date=business_date,
            value=value, now=now, workdays=workdays,
        ))
    lines = [
        f"✅ 已代录（管理人 {name} 为 {target_name} 补签 "
        f"{business_date.month}月{business_date.day}日）："
    ]
    for cell, value, old_disp, progress in writes:
        line = f"{cell}：{value}"
        if old_disp:
            line += f"（🔁 覆盖旧值 {old_disp}）"
        lines.append(line)
        progress_line = _progress_line(cell, progress)
        if progress_line:
            lines.append(progress_line)
    return IntakeOutcome(
        "recorded", "\n".join(lines),
        region=region_cfg.region, name=name,
        value=[w[1] for w in writes],
        overwritten=any(w[2] is not None for w in writes),
    )


def _handle_aux_command(connection, aux, *, region_cfg, member, sender_uid,
                        name, sender_dept, root_dept, business_date,
                        workdays, now, all_region_cfgs=None):
    command, arg = aux
    if command == "帮助":
        hint_store = sender_dept if sender_dept in _CANONICAL_STORES else None
        hint_stores = (
            sorted(set(_STORE_ALIASES.values()))
            if sender_dept and sender_dept == root_dept and not hint_store
            else ()
        )
        return IntakeOutcome(
            "aux", build_aux_help(name, store=hint_store, stores=hint_stores),
            region=region_cfg.region, name=name,
        )
    if command == "未填":
        return IntakeOutcome(
            "aux",
            _aux_unfilled(connection, region_cfg=region_cfg,
                          business_date=business_date),
            region=region_cfg.region, name=name,
        )
    if command == "我的":
        return IntakeOutcome(
            "aux",
            _aux_mine(connection, region_cfg=region_cfg, name=name,
                      sender_dept=sender_dept, business_date=business_date,
                      workdays=workdays),
            region=region_cfg.region, name=name,
        )
    if command == "门店":
        return IntakeOutcome(
            "aux",
            _aux_stores(
                connection, region_cfg=region_cfg,
                business_date=business_date, workdays=workdays,
                all_region_cfgs=all_region_cfgs,
                is_admin=_is_admin(connection, sender_uid),
            ),
            region=region_cfg.region, name=name,
        )
    return _aux_backfill(connection, arg, region_cfg=region_cfg,
                         sender_uid=sender_uid, name=name, now=now)


def handle_report(connection, *, region_cfg, text, sender_uid, now,
                  all_region_cfgs=None):
    """处理一条报数消息。不写连接 commit（由调用方提交，与其他流一致）。"""
    business_date = now.date() if isinstance(now, datetime) else now
    workdays = fetch_workdays(connection, year=now.year, month=now.month)
    if not workdays:
        raise MartTaskError(
            f"dim_calendar 缺少 {now:%Y-%m} 的日历行，请先运行 extract-mart"
        )
    if business_date not in workdays:
        return IntakeOutcome("not_workday", build_not_workday_reply(),
                             region=region_cfg.region)

    member = fetch_member(connection, user_id=sender_uid)
    via_admin = (
        member is not None
        and member["region"] != region_cfg.region
        and _is_admin(connection, sender_uid)
    )
    # admin grant 例外（bi_authz_grant grant_type='admin'）：跨区报数
    # 用于运维测试与代录，仍要求 dim 有档案（署名/部门归属需要）。
    if member is None or (member["region"] != region_cfg.region and not via_admin):
        return IntakeOutcome("not_member", build_not_member_reply(),
                             region=region_cfg.region)

    name = member["name"]
    sender_dept = member.get("dept_name")
    root_dept = region_cfg.dept_order[0] if region_cfg.dept_order else None

    # “/” 辅助指令优先于报数解析（如「/补签 张三 12800」含数字，不能误录）。
    aux = parse_aux_command(text)
    if aux is not None:
        return _handle_aux_command(
            connection, aux,
            region_cfg=region_cfg, member=member, sender_uid=sender_uid,
            name=name, sender_dept=sender_dept, root_dept=root_dept,
            business_date=business_date, workdays=workdays, now=now,
            all_region_cfgs=all_region_cfgs,
        )

    # 多板块报数（vanke 模型：数据格 = 门店 × {零售,团购}，权限 = 部门归属）。
    entries = parse_report_metrics(text)
    if entries is not None:
        writes = []
        for store_label, metric, value, recognized_from in entries:
            store = store_label or sender_dept
            if not store:
                return IntakeOutcome(
                    "no_number", build_format_hint(name),
                    region=region_cfg.region, name=name,
                )
            # 点名门店时：本部门只能报本店；根部门（dept_order[0]）可跨店。
            if (
                store_label
                and sender_dept != root_dept
                and store_label != sender_dept
            ):
                return IntakeOutcome(
                    "not_member",
                    build_store_denied_reply(name, sender_dept, store_label),
                    region=region_cfg.region, name=name,
                )
            cell = f"{store}·{metric}"
            old_value = _write_report(
                connection,
                region=region_cfg.region,
                member=member,
                table_name=cell,
                monthly_target=region_cfg.monthly_targets.get(cell),
                business_date=business_date,
                value=value,
                now=now,
                key_suffix=metric,
            )
            progress = _progress(
                connection,
                region=region_cfg.region,
                table_name=cell,
                business_date=business_date,
                workdays=workdays,
            )
            overwritten = old_value is not None and float(old_value) != float(value)
            note = f"（识别：{recognized_from}）" if recognized_from else None
            writes.append(
                (
                    cell,
                    value,
                    _fmt_amount(old_value) if overwritten else None,
                    progress,
                    note,
                )
            )
        reply = build_multi_recorded_reply(
            month=now.month,
            day=business_date.day,
            weekday=_WEEKDAYS[business_date.weekday()],
            writes=writes,
        )
        return IntakeOutcome(
            "recorded", reply,
            region=region_cfg.region, name=name,
            value=[w[1] for w in writes],
            overwritten=any(w[2] is not None for w in writes),
        )

    value = parse_report_amount(text)
    if value is None:
        # 个性化格式提示（自动补全 B）：本店成员给本店示例，根部门给门店列表。
        hint_store = sender_dept if sender_dept in _CANONICAL_STORES else None
        hint_stores = (
            sorted(set(_STORE_ALIASES.values()))
            if sender_dept and sender_dept == root_dept and not hint_store
            else ()
        )
        return IntakeOutcome(
            "no_number",
            build_format_hint(name, store=hint_store, stores=hint_stores),
            region=region_cfg.region, name=name,
        )

    table_name = region_cfg.aliases.get(name, name)
    old_value = _write_report(
        connection,
        region=region_cfg.region,
        member=member,
        table_name=table_name,
        # 无 AI 表区域的月目标快照（有表区域为 None，目标由表行携带）。
        monthly_target=region_cfg.monthly_targets.get(table_name),
        business_date=business_date,
        value=value,
        now=now,
    )

    progress = _progress(
        connection,
        region=region_cfg.region,
        table_name=table_name,
        business_date=business_date,
        workdays=workdays,
    )
    overwritten = old_value is not None and float(old_value) != float(value)
    reply = build_recorded_reply(
        month=now.month,
        day=business_date.day,
        weekday=_WEEKDAYS[business_date.weekday()],
        value=value,
        old_value=_fmt_amount(old_value) if overwritten else None,
        progress=progress,
    )
    return IntakeOutcome(
        "recorded", reply,
        region=region_cfg.region, name=name, value=value,
        overwritten=overwritten,
    )


# ---------------------------------------------------------------------------
# 写入与进度
# ---------------------------------------------------------------------------

def _fetch_one(connection, sql, params):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else row


def _is_admin(connection, user_id):
    """bi_authz_grant 中存在该用户的 admin grant（报数门禁的唯一例外通道）。"""
    row = _fetch_one(
        connection,
        "SELECT 1 AS `x` FROM `bi_authz_grant` "
        "WHERE `user_id` = %s AND `grant_type` = 'admin' LIMIT 1",
        (user_id,),
    )
    return row is not None


def _write_report(connection, *, region, member, table_name,
                  monthly_target, business_date, value, now, key_suffix=None):
    """业务键优先写入。返回旧值（无旧行或旧值为空 → None）。

    *key_suffix*：多板块报数时追加到 ``source_record_id`` 末尾（同一成员
    同日多数据格各一行）；单金额旧路径不传，保持原键格式不变。
    """
    existing = _fetch_one(
        connection,
        "SELECT `source_record_id`, `sales_amount` "
        "FROM `fact_daily_report_offline` "
        "WHERE `region` = %s AND `responsible_person` = %s "
        "AND `business_date` = %s "
        "ORDER BY (`monthly_target` IS NULL) LIMIT 1",
        (region, table_name, business_date),
    )
    with contextlib.closing(connection.cursor()) as cursor:
        if existing is not None:
            cursor.execute(
                "UPDATE `fact_daily_report_offline` "
                "SET `sales_amount` = %s, `synced_at` = %s "
                "WHERE `source_record_id` = %s",
                (value, now, existing["source_record_id"]),
            )
            return existing.get("sales_amount")

        cursor.execute(
            "INSERT INTO `fact_daily_report_offline` "
            "(`source_record_id`, `region`, `responsible_person`, "
            "`department`, `business_date`, `sales_amount`, `monthly_target`, "
            "`synced_at`, `sync_run_id`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                f"stream:{region}:{member['user_id']}:{business_date.isoformat()}"
                + (f":{key_suffix}" if key_suffix else ""),
                region,
                table_name,
                member.get("dept_name"),
                business_date,
                value,
                monthly_target,
                now,
                STREAM_RUN_ID,
            ),
        )
    return None


def _progress(connection, *, region, table_name, business_date, workdays):
    """本月累计 / 目标 / 完成率（含当天；共享口径）。无有效目标 → None。"""
    elapsed = elapsed_workdays(workdays, today=business_date, include_today=True)
    facts = fetch_month_facts(
        connection, region=region,
        year=business_date.year, month=business_date.month,
    )
    rows = [
        row for row in facts
        if str(row.get("responsible_person") or "").strip() == table_name
    ]
    summary = summarize_people(rows, elapsed_days=elapsed).get(table_name)
    if summary is None or summary.rate is None:
        return None
    total_disp = _fmt_amount(summary.completed)
    target_disp = _fmt_amount(summary.target)
    return total_disp, target_disp, f"{summary.rate * 100:.1f}%"


def _fmt_amount(value):
    number = float(value)
    if number == int(number):
        return str(int(number))
    return str(round(number, 2))
