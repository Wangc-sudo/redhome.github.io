# -*- coding: utf-8 -*-
"""
工作日历工具（数字中台公共层）
==============================
统一各机器人的「休息日/工作日」判定口径。

日历来源（``calendar.source``）：
    local          随 config 下发的当月休息日（现行默认，由规则生成）
    yonyou_tplus   用友 T+ 排班（框架已就绪）——业务组排班特殊，由 T+ 承载；
                   填好 ``yonyouTplus`` 凭据后即可切换，报文口径待实测校准

配置形态::

    "calendar": {
      "month": 9,
      "source": "local",
      "restDays": [6, 13, 19, 25, 26, 27],
      "rule": {                      # 可选：restDays 的生成依据，二者须一致
        "bigRestSaturdays": [19],    # 大休周的周六（小休周周六上班）
        "holidays": [25, 26, 27],    # 法定节假日
        "makeupWorkdays": [20]       # 调休上班（从休息日中剔除）
      },
      "yonyouTplus": {               # T+ 凭据（source=yonyou_tplus 时必填）
        "baseUrl": "", "appKey": "", "appSecret": "", "tenantId": "", "schemeId": ""
      }
    }

用法::

    from common.calendar_utils import Calendar
    cal = Calendar(month=9, rest_days=[6, 13, 19, 25, 26, 27])
    cal = Calendar.from_config(config["calendar"])
    cal.check_month()          # 当月不符抛 CalendarError
    cal.workdays()             # 全月工作日列表
    cal.is_rest(day)           # 是否休息日
    cal.elapsed(include_today) # 已过工作日列表（早报口径）

休息日由规则推导（避免手工列举漂移）::

    from common.calendar_utils import generate_rest_days
    generate_rest_days(2026, 9, big_rest_saturdays=[19],
                       holidays=[25, 26, 27], makeup_workdays=[20])
    # -> [6, 13, 19, 25, 26, 27]
"""
import calendar as _pycal
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_SAT = 5
_WEEKDAY_SUN = 6

SOURCE_LOCAL = "local"
SOURCE_YONYOU_TP = "yonyou_tplus"

#: 规则兜底行的 source 标记（2026-10-10 三层防线 A 层）：种子未覆盖的
#: 近端月份按「周日休 + 每月第 3 个周六大休」纯规则合成（沿用 2026 种子
#: 锚点约定）——翻年/翻月不再 MartTaskError 硬死（法定节假日口径失真由
#: B 层 calendar-watch 管道告警补救）。
RULE_FALLBACK_SOURCE = "rule_fallback"

#: 兜底窗口：以今天为锚的 [前 2 个月, 后 3 个月]（上月环比/当月/下月
#: 页面导航都在覆盖内；每日 extract 滚动刷新，窗口随时间自走）。
_FALLBACK_LOOKBACK = 2
_FALLBACK_LOOKAHEAD = 3

_CALENDAR_SEED_VERSION = 1


class CalendarError(RuntimeError):
    pass


class CalendarSourceUnavailable(CalendarError):
    """日历数据源尚未接入（占位数据源被调用时抛此异常）。"""


def generate_rest_days(year, month, big_rest_saturdays=(), holidays=(), makeup_workdays=()):
    """按「大小休 + 法定节假日 + 调休」规则生成当月休息日号列表（升序）。

    规则（与现行 config 的 restDays 逐位对齐）：
      1. 基准：每周日休息 —— 小休周只休周日，周六上班；
      2. 大休周：``big_rest_saturdays`` 指定的周六一并休息（周日已在基准内）；
      3. 法定节假日：``holidays`` 直接计入休息；
      4. 调休上班：``makeup_workdays`` 从休息集合中剔除。

    ```bigRestSaturdays=[19] + holidays=[25,26,27] - makeupWorkdays=[20]``
    即得 2026-09 的 ``[6, 13, 19, 25, 26, 27]``。

    :param year: 年份
    :param month: 月份
    :param big_rest_saturdays: 大休周的周六日号，须为当月真实的周六
    :param holidays: 法定节假日日号
    :param makeup_workdays: 调休上班日号
    :return: 升序休息日号列表
    """
    year, month = int(year), int(month)
    last_day = _pycal.monthrange(year, month)[1]

    def _checked(days, label):
        out = []
        for raw in days:
            d = int(raw)
            if not 1 <= d <= last_day:
                raise CalendarError(
                    f"{label} 含 {d}，超出 {year}-{month:02d} 当月范围(1~{last_day})")
            out.append(d)
        return out

    saturdays = _checked(big_rest_saturdays, "bigRestSaturdays")
    holidays = _checked(holidays, "holidays")
    makeup_workdays = _checked(makeup_workdays, "makeupWorkdays")

    rest = {d for d in range(1, last_day + 1)
            if datetime(year, month, d).weekday() == _WEEKDAY_SUN}
    for d in saturdays:
        if datetime(year, month, d).weekday() != _WEEKDAY_SAT:
            raise CalendarError(
                f"bigRestSaturdays 含 {year}-{month:02d}-{d:02d}，该日不是周六")
        rest.add(d)
    rest.update(holidays)
    rest.difference_update(makeup_workdays)
    return sorted(rest)


def check_rule_matches_rest_days(cal_cfg, year=None):
    """校验 ``calendar.rule`` 推导的休息日与 ``restDays`` 一致。

    返回差异提示列表（空列表表示一致或未配置 rule）。
    """
    rule = cal_cfg.get("rule")
    if not rule:
        return []
    year = int(rule.get("year") or year or datetime.now().year)
    derived = generate_rest_days(
        year,
        cal_cfg["month"],
        rule.get("bigRestSaturdays", []),
        rule.get("holidays", []),
        rule.get("makeupWorkdays", []),
    )
    declared = sorted(int(d) for d in cal_cfg.get("restDays", []))
    if derived == declared:
        return []
    return [
        f"calendar.rule 推导 {year}-{int(cal_cfg['month']):02d} 休息日={derived}，"
        f"与 restDays={declared} 不一致"
    ]


def month_days(year, month):
    """返回该年月的全部日期（``datetime.date``，升序）。

    与 :class:`Calendar` 的 1~30 简化口径不同，这里按真实月份长度取数，
    供 ``dim_calendar`` 这类需要逐日落库的场景使用。
    """
    year, month = int(year), int(month)
    last_day = _pycal.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last_day + 1)]


def _shift_month(year, month, delta):
    """``(year, month)`` 平移 *delta* 个月（可负）。"""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def synthesize_fallback_months(covered, *, today=None):
    """规则兜底月（A 层）：窗口内种子未覆盖的月份按「周日休 + 每月第 3
    个周六大休」纯规则合成（沿用 2026 种子锚点约定）。

    *covered* 为已被种子覆盖的 ``(year, month)`` 集合；返回
    ``[(year, month, rest_days, RULE_FALLBACK_SOURCE)]``（按年月升序）。
    法定节假日未知一律按工作日计——宁可口径失真不可静默死；失真月份由
    :data:`RULE_FALLBACK_SOURCE` 落进 ``dim_calendar.source``，
    calendar-watch 管道据此告警。
    """
    today = today or date.today()
    covered = set(covered)
    rows = []
    for delta in range(-_FALLBACK_LOOKBACK, _FALLBACK_LOOKAHEAD + 1):
        year, month = _shift_month(today.year, today.month, delta)
        if (year, month) in covered:
            continue
        saturdays = [
            d.day for d in month_days(year, month) if d.weekday() == _WEEKDAY_SAT
        ]
        rows.append((
            year,
            month,
            generate_rest_days(
                year, month, big_rest_saturdays=[saturdays[2]]
            ),
            RULE_FALLBACK_SOURCE,
        ))
    return rows


def load_calendar_seed(path, *, fallback=True, today=None):
    """解析版本受控的日历种子，返回 ``[(year, month, rest_days, source)]``。

    *fallback*（2026-10-10 A 层，缺省 True）：在种子月份之后追加
    :func:`synthesize_fallback_months` 的规则兜底月（窗口以 *today* 为锚，
    缺省当天）——``dim_calendar`` 近端月份自此永不空表；读原始种子内容
    （如 calendar-import 自证）传 ``fallback=False``。

    种子形态::

        {
          "version": 1,
          "source": "local",
          "months": [
            {"year": 2026, "month": 9,
             "bigRestSaturdays": [19],
             "holidays": [25, 26, 27],
             "makeupWorkdays": [20]}
          ]
        }

    ``rest_days`` 一律由 :func:`generate_rest_days` 推导、**不手工列举**，所以
    种子只记录*意图*（rule），渲染结果不可能与规则漂移。

    ``source`` 缺省 :data:`SOURCE_LOCAL`。用友 T+ 尚未接入，声明它即抛
    :class:`CalendarSourceUnavailable`——**明确失败，不静默降级**；未知来源
    抛 :class:`CalendarError`，避免静默走错口径。

    口径分层（2026-10-10 运维裁决）：本种子只承载**规则基线**（历史月
    法定假 + 大小休锚点）；**播报口径裁决层**已迁入 DB
    ``dim_calendar_override``（``calendar_store``，法定导入 + ops-web
    页面人工裁决）——JSON 不再承载逐日裁决，导入与页面操作都不碰本
    文件（2026-10-10 起，原 broadcastAdjust JSON 层随裁决行迁库废弃）。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarError("日历种子不是可读的 JSON 文件") from exc
    if not isinstance(raw, dict):
        raise CalendarError("日历种子必须是 JSON 对象")

    version = raw.get("version")
    if version != _CALENDAR_SEED_VERSION:
        raise CalendarError(
            f"日历种子 version 必须为 {_CALENDAR_SEED_VERSION}（当前 {version!r}）")

    source = (raw.get("source") or SOURCE_LOCAL).strip()
    if source == SOURCE_YONYOU_TP:
        raise CalendarSourceUnavailable(
            "日历种子不承载 source=yonyou_tplus：种子只记录 local 规则基线，"
            "T+ 排班由运行时 YonyouTplusSchedule 取数、不进种子文件。")
    if source != SOURCE_LOCAL:
        raise CalendarError(
            f"未知的 calendar.source={source!r}，"
            f"仅支持 {SOURCE_LOCAL!r} / {SOURCE_YONYOU_TP!r}")

    months = raw.get("months")
    if not isinstance(months, list) or not months:
        raise CalendarError("日历种子 months 必须是非空数组")

    result = []
    seen = set()
    for index, item in enumerate(months):
        label = f"months[{index}]"
        if not isinstance(item, dict):
            raise CalendarError(f"日历种子 {label} 必须是对象")
        try:
            year = int(item["year"])
            month = int(item["month"])
        except (KeyError, TypeError, ValueError):
            raise CalendarError(f"日历种子 {label} 需要整数 year / month") from None
        if not 1 <= month <= 12:
            raise CalendarError(f"日历种子 {label}.month 必须为 1..12")
        if (year, month) in seen:
            raise CalendarError(f"日历种子 {label} 重复声明 {year}-{month:02d}")
        seen.add((year, month))

        result.append((
            year,
            month,
            generate_rest_days(
                year,
                month,
                item.get("bigRestSaturdays", []),
                item.get("holidays", []),
                item.get("makeupWorkdays", []),
            ),
            source,
        ))
    if fallback:
        result.extend(synthesize_fallback_months(seen, today=today))
    return result


class CalendarSource:
    """工作日历来源契约。"""

    name = "abstract"

    def load(self, year, month):
        """返回该年月的休息日号列表（升序）。"""
        raise NotImplementedError


class LocalRestDays(CalendarSource):
    """随 config 下发的当月休息日（现行默认）。

    在用友 T+ 排班源接入前，由它代替 T+ 提供工作日口径；``restDays`` 建议由
    :func:`generate_rest_days` 生成，并以 ``calendar.rule`` 记录生成依据。
    """

    name = SOURCE_LOCAL

    def __init__(self, cal_cfg):
        self.month = int(cal_cfg["month"])
        self._rest = sorted(int(d) for d in cal_cfg.get("restDays", []))

    def load(self, year, month):
        return list(self._rest)


def _urllib_json_transport(method, url, headers=None, payload=None, timeout=10):
    """缺省 HTTP 传输：标准库 urllib，返回解析后的 JSON dict。

    供 :class:`YonyouTplusSchedule` 使用；测试可注入假 transport。
    """
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class YonyouTplusSchedule(CalendarSource):
    """用友 T+ 排班源（框架已就绪，凭据填好后即可切换）。

    业务组的排班比较特殊，不由钉钉考勤承载——实测 2026 年 6/7/8/9 四个月钉钉排班
    记录恒为 0（详见 spec §10），故改由用友 T+ 承载，作为工作日口径的长期真源。

    已钉死的结构（不再改动）：
      - 凭据缺失 → :class:`CalendarSourceUnavailable`（明确失败，不静默降级）；
      - token 获取后按 6 天有效期缓存复用，避免每次取数重复换 token；
      - 取数结果按 ``(year, month)`` 缓存，单进程内同月不重复请求；
      - T+ 请求失败 → 回退 ``calendar.restDays`` 并告警（``alarm`` 回调，
        缺省 ``logger.warning``）；无 restDays 可回退时仍明确失败。

    待实测钉死（凭据就绪后校准，改动点已隔离）：
      - ``_TOKEN_PATH`` / ``_SCHEDULE_PATH``：以 T+ 开放平台实际接口路径为准；
      - :meth:`_build_auth_headers`：签名算法以 T+ 文档为准；
      - :meth:`_parse_schedule_payload`：以 T+ 真实排班报文字段为准。
    """

    name = SOURCE_YONYOU_TP

    #: 凭据必填键（缺一即明确失败，报错列出缺项）
    REQUIRED_KEYS = ("baseUrl", "appKey", "appSecret", "tenantId", "schemeId")

    # 待实测钉死：以下为按 T+ OpenAPI 通行惯例的占位路径
    _TOKEN_PATH = "/auth/getAppToken"
    _SCHEDULE_PATH = "/tplus/api/v1/schedule/query"
    _TOKEN_TTL_SECONDS = 6 * 24 * 3600  # 平台颁发 openToken 有效期 6 天
    _TOKEN_REFRESH_MARGIN = 3600        # 提前 1 小时换 token，避免边界过期

    def __init__(self, tp_cfg=None, *, fallback_cfg=None, transport=None, alarm=None):
        self.cfg = dict(tp_cfg or {})
        self._fallback_cfg = fallback_cfg or {}
        self._transport = transport or _urllib_json_transport
        self._alarm = alarm or _LOGGER.warning
        self._cache = {}            # (year, month) -> [rest_days]
        self._token = None
        self._token_obtained_at = 0.0

    # -- 契约入口 ----------------------------------------------------

    def load(self, year, month):
        year, month = int(year), int(month)
        cached = self._cache.get((year, month))
        if cached is not None:
            return list(cached)
        self._check_credentials()
        try:
            rest_days = self._fetch_rest_days(year, month)
        except CalendarSourceUnavailable:
            raise
        except Exception as exc:  # T+ 不可用：回退 local 并告警，不静默
            return self._fallback(year, month, exc)
        self._cache[(year, month)] = list(rest_days)
        return list(rest_days)

    # -- 凭据 ---------------------------------------------------------

    def _check_credentials(self):
        missing = [k for k in self.REQUIRED_KEYS
                   if not str(self.cfg.get(k) or "").strip()]
        if missing:
            raise CalendarSourceUnavailable(
                "用友 T+ 排班源凭据未就绪：calendar.yonyouTplus 缺 "
                + "、".join(missing)
                + "；请补齐凭据后切换，或保持 source=local 由 restDays + rule 兜底。")

    # -- token --------------------------------------------------------

    def _url(self, path):
        return self.cfg["baseUrl"].rstrip("/") + path

    def _build_auth_headers(self):
        """构造签名头。待实测钉死：以 T+ 开放平台签名文档校准。"""
        timestamp = str(int(time.time()))
        raw = f"{self.cfg['appKey']}{timestamp}{self.cfg['appSecret']}"
        return {
            "appKey": self.cfg["appKey"],
            "timestamp": timestamp,
            "sign": hashlib.md5(raw.encode("utf-8")).hexdigest(),
        }

    def _ensure_token(self):
        now = time.time()
        if self._token and now - self._token_obtained_at < (
                self._TOKEN_TTL_SECONDS - self._TOKEN_REFRESH_MARGIN):
            return self._token
        payload = self._transport(
            "POST", self._url(self._TOKEN_PATH),
            headers=self._build_auth_headers(),
            payload={"appKey": self.cfg["appKey"],
                     "appSecret": self.cfg["appSecret"]},
        )
        token = (payload or {}).get("access_token") or (payload or {}).get("accessToken")
        if not token:
            raise CalendarSourceUnavailable(
                f"T+ token 响应缺 access_token：{str(payload)[:200]}")
        self._token = token
        self._token_obtained_at = now
        return token

    # -- 排班取数 ------------------------------------------------------

    def _fetch_rest_days(self, year, month):
        token = self._ensure_token()
        payload = self._transport(
            "POST", self._url(self._SCHEDULE_PATH),
            headers={"Authorization": f"Bearer {token}"},
            payload={"tenantId": self.cfg["tenantId"],
                     "schemeId": self.cfg["schemeId"],
                     "year": year, "month": month},
        )
        return self._parse_schedule_payload(payload, year, month)

    @staticmethod
    def _parse_schedule_payload(payload, year, month):
        """T+ 排班报文 → 休息日号列表（升序）。待实测钉死：按真实报文校准。

        当前假设口径：``{"days": [{"date": "YYYY-MM-DD", "isRest": true}, ...]}``，
        isRest 为真计入休息日；仅收当月数据，跨月日期忽略；日期不可解析的行跳过。
        """
        days = (payload or {}).get("days")
        if not isinstance(days, list):
            raise CalendarSourceUnavailable(
                f"T+ 排班响应缺 days 数组：{str(payload)[:200]}")
        rest = set()
        for item in days:
            try:
                d = date.fromisoformat(str(item["date"]))
            except (KeyError, TypeError, ValueError):
                continue
            if d.year == year and d.month == month and item.get("isRest"):
                rest.add(d.day)
        return sorted(rest)

    # -- 回退 ----------------------------------------------------------

    def _fallback(self, year, month, exc):
        rest = self._fallback_cfg.get("restDays")
        if not rest:
            raise CalendarSourceUnavailable(
                f"T+ 排班拉取失败且无 local restDays 可回退：{exc}") from exc
        fallback = sorted(int(d) for d in rest)
        self._alarm(
            f"用友 T+ 排班拉取失败（{type(exc).__name__}: {exc}），"
            f"{year}-{month:02d} 回退 local restDays={fallback}")
        return fallback


def calendar_source(cal_cfg):
    """按 ``calendar.source`` 构造数据源；未知来源直接报错，避免静默走错口径。"""
    name = (cal_cfg.get("source") or SOURCE_LOCAL).strip()
    if name == SOURCE_LOCAL:
        return LocalRestDays(cal_cfg)
    if name == SOURCE_YONYOU_TP:
        return YonyouTplusSchedule(
            cal_cfg.get("yonyouTplus"), fallback_cfg=cal_cfg)
    raise CalendarError(
        f"未知的 calendar.source={name!r}，仅支持 {SOURCE_LOCAL!r} / {SOURCE_YONYOU_TP!r}")


class Calendar:
    def __init__(self, month, rest_days):
        self.month = int(month)
        self.rest_days = set(int(d) for d in rest_days)
        # 简化口径：1~30 日；如遇 31 天月份需在业务侧确认
        self._workdays = [d for d in range(1, 31) if d not in self.rest_days]

    @classmethod
    def from_config(cls, cal_cfg, year=None, now=None):
        """从 config.json 的 calendar 段构造（``calendar.source`` 分派，缺省 local）。

        走用友 T+ 源且尚未接入时抛 :class:`CalendarSourceUnavailable`。
        """
        month = int(cal_cfg["month"])
        year = int(year or (now or datetime.now()).year)
        return cls(month, calendar_source(cal_cfg).load(year, month))

    def check_month(self, now=None):
        """当前月份与配置不符时抛 CalendarError（换月需建新表更新配置）"""
        now = now or datetime.now()
        if now.month != self.month:
            raise CalendarError(
                f"当前月份{now.month}与配置月份{self.month}不符，请为新月建表并更新 config")

    def workdays(self):
        return list(self._workdays)

    def is_rest(self, day):
        return day in self.rest_days

    def elapsed(self, include_today=False, now=None):
        """已过工作日列表。默认不含今日（早报口径：18点前只统计截至昨日）"""
        now = now or datetime.now()
        return [d for d in self._workdays
                if d < now.day or (include_today and d == now.day)]

    def today_state(self, now=None):
        """返回 (now, day, err)。day=None 表示休息日/跨月不可用，err 为提示文本"""
        now = now or datetime.now()
        try:
            self.check_month(now)
        except CalendarError as e:
            return now, None, str(e)
        if self.is_rest(now.day):
            return now, None, None
        return now, now.day, None

    @property
    def total(self):
        return len(self._workdays)
