"""BI offline_dashboards 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *
from common.bi_web.query_domains.offline import *
from common.bi_web.query_domains.people import *


_HZ_REGION = "hangzhou"


_WORKDAY_OPTIONS_SQL = (
    "SELECT `business_date` FROM `dim_calendar` "
    "WHERE `is_workday` = 1 AND `business_date` <= CURDATE() "
    "ORDER BY `business_date`"
)


_HZ_TODAY_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) AS `s` "
    "FROM `fact_daily_report_offline` "
    "WHERE `region` = 'hangzhou' AND `business_date` = %s "
    "AND `responsible_person` NOT LIKE '%%合计%%'"
)


def workday_options(connection) -> list:
    """筛选器「日期」日历点选值域：已历工作日 ISO 串升序。

    前端月历按月渲染、蓝=工作日；未来日/非工作日不在值域（上游值闸
    fail-open，非法值只会 400 不会注入）。
    """
    return [
        str(row["business_date"])
        for row in _fetch_rows(connection, _WORKDAY_OPTIONS_SQL)
        if row["business_date"]
    ]


def _hz_anchor(params):
    """杭州看板锚点日：``date`` 参数（YYYY-MM-DD），缺省/非法回退今天
    （容器本地时钟；值域校验在 app 层，这里只兜底解析）。"""
    raw = params.get("date")
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now().date()


def run_kpi_hz_mtd(connection, params) -> dict:
    """杭州本月累计 + 达成率（scalar）：kpi_region_mtd 的杭州锚定版
    （月 = 锚点日所在月）。"""
    anchor = _hz_anchor(params)
    return run_kpi_region_mtd(
        connection,
        _carry_scope(params, {"region": _HZ_REGION, "month": anchor.strftime("%Y-%m")}),
    )


def run_kpi_hz_today(connection, params) -> dict:
    """杭州当日销售（scalar）：锚点日 Σ sales_amount（排合计行）。

    行级 scope（M3）：语句已参数化（字面 % 双写），scope 非 None 时在
    末尾追加 ``region IN`` 片段——钉死区域不在 scope 内即 0（fail-closed）。
    """
    anchor = _hz_anchor(params)
    fragment, scope_params = _scope_region_filter(scope_from_params(params))
    rows = _fetch_rows(
        connection, _append_region_sql(_HZ_TODAY_SQL, fragment), (anchor,) + scope_params
    )
    value = rows[0]["s"] if rows else 0
    return {
        "chart": "scalar",
        "value": float(value),
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_kpi_hz_progress(connection, params) -> dict:
    """杭州时间进度（scalar）：已过工作日 ÷ 全月工作日（dim_calendar）。

    value=已过、target=全月、rate=进度；含锚点当天（BI 实时口径）。
    """
    anchor = _hz_anchor(params)
    workdays = fetch_workdays(connection, year=anchor.year, month=anchor.month)
    elapsed = elapsed_workdays(workdays, today=anchor, include_today=True)
    total = len(workdays)
    return {
        "chart": "scalar",
        "value": float(len(elapsed)),
        "target": float(total),
        "rate": (len(elapsed) / total) if total else None,
        "unit": "个工作日",
        "date": anchor.isoformat(),
    }


def run_bar_hz_dept(connection, params) -> dict:
    """杭州部门本月排行（bar）：bar_department_mtd 的杭州锚定版。"""
    anchor = _hz_anchor(params)
    return run_bar_department_mtd(
        connection,
        _carry_scope(params, {"region": _HZ_REGION, "month": anchor.strftime("%Y-%m")}),
    )


def run_trend_hz_daily(connection, params) -> dict:
    """杭州日销趋势（line）：trend_region_daily 的杭州锚定版（月=锚点月，
    gran 透传，缺省日）。"""
    anchor = _hz_anchor(params)
    return run_trend_region_daily(
        connection,
        _carry_scope(params, {
            "region": _HZ_REGION,
            "month": anchor.strftime("%Y-%m"),
            "gran": params.get("gran") or "day",
        }),
    )


def _unfilled_payload(connection, region, anchor, scope=None):
    """区域当日未填报人载荷（hz/sx 共用，2026-10-10 运维反馈新增）。

    真源 = 催办同款 ``mart_tasks._unfilled``（dim_robot_member ∩
    名册绑定 − 当日已填；名册无记录 fail-open）——看板名单与 18:00
    提醒/20:00 催办逐字一致，不做第二套口径。aliases 传空：Nacos
    aliases 是机器人通道的表内用名补丁（2026-10-08 已清零），BI 卡
    不挂区域配置。
    """
    from common.daily_robot.mart_tasks import _unfilled

    members = (
        _unfilled(connection, region, anchor, {})
        if _scope_allows_regions(scope, region) else []
    )
    rows = [
        {
            "rank": index,
            "name": member["name"],
            "dept": member.get("dept_name") or "未分组",
        }
        for index, member in enumerate(members, start=1)
    ]
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "#"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
        ],
        "rows": rows,
        "date": anchor.isoformat(),
    }


def run_table_hz_unfilled_today(connection, params) -> dict:
    """杭州当日未填报人（table）：``_unfilled_payload`` 杭州锚定版。"""
    return _unfilled_payload(
        connection, _HZ_REGION, _hz_anchor(params), scope_from_params(params)
    )


def _people_progress_payload(connection, region, anchor, scope=None):
    """区域个人完成率榜载荷（hz/sx 共用）：静态榜单页同口径全列。

    排名/姓名/部门/完成/月目标/达成率/进度差/日均完成/预计月末/未填；
    进度基准 = 已过工作日 ÷ 全月工作日（mart_collect 自带 elapsed，
    含锚点当天）。dim_calendar 缺行（MartTaskError）降级空表。
    """
    if not _scope_allows_regions(scope, region):
        data = None  # 行级 scope 门：钉死区域不授权，整表空（fail-closed）
    else:
        try:
            data = mart_collect(
                connection, region=region, business_date=anchor,
                include_today=True,
            )
        except MartTaskError:
            data = None
    rows = []
    if data is not None and data.workdays:
        n_elapsed = len(data.elapsed)
        progress = n_elapsed / len(data.workdays)
        for index, person in enumerate(data.people, start=1):
            rate = person["rate"]
            rows.append({
                "rank": index,
                "name": person["name"],
                "dept": person["dept"],
                "completed": float(person["completed"]),
                "target": float(person["target"]),
                "rate": rate,
                "diff": (rate - progress) if rate is not None else None,
                "daily": (float(person["completed"]) / n_elapsed
                          if n_elapsed else 0.0),
                "proj": (rate / progress
                         if rate is not None and progress > 0 else None),
                "unfilled": person["unfilled"],
            })
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "排名"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
            {"key": "completed", "title": "完成额", "format": "wan"},
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "rate", "title": "达成率", "format": "percent"},
            {"key": "diff", "title": "进度差", "format": "percent"},
            {"key": "daily", "title": "日均完成", "format": "wan"},
            {"key": "proj", "title": "预计月末", "format": "percent"},
            {"key": "unfilled", "title": "未填"},
        ],
        "rows": rows,
        "date": anchor.isoformat(),
    }


def run_table_hz_people_progress(connection, params) -> dict:
    """杭州个人完成率榜（table）：``_people_progress_payload`` 杭州锚定版。"""
    return _people_progress_payload(
        connection, _HZ_REGION, _hz_anchor(params), scope_from_params(params)
    )


_SX_REGION = "shaoxing"


_SX_TODAY_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) AS `s` "
    "FROM `fact_daily_report_offline` "
    "WHERE `region` = 'shaoxing' AND `business_date` = %s "
    "AND `responsible_person` NOT LIKE '%%合计%%'"
)


def run_kpi_sx_mtd(connection, params) -> dict:
    """绍兴本月累计 + 达成率（scalar）：kpi_region_mtd 的绍兴锚定版。"""
    anchor = _hz_anchor(params)
    return run_kpi_region_mtd(
        connection,
        _carry_scope(params, {"region": _SX_REGION, "month": anchor.strftime("%Y-%m")}),
    )


def run_kpi_sx_today(connection, params) -> dict:
    """绍兴当日销售（scalar）：锚点日 Σ sales_amount（排合计行）。

    行级 scope（M3）：与杭州卡同款——语句已参数化，scope 非 None 时
    末尾追加 ``region IN`` 片段（钉死区域不授权即 0）。
    """
    anchor = _hz_anchor(params)
    fragment, scope_params = _scope_region_filter(scope_from_params(params))
    rows = _fetch_rows(
        connection, _append_region_sql(_SX_TODAY_SQL, fragment), (anchor,) + scope_params
    )
    value = rows[0]["s"] if rows else 0
    return {
        "chart": "scalar",
        "value": float(value),
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_kpi_sx_progress(connection, params) -> dict:
    """绍兴时间进度（scalar）：与杭州卡同函数——工作日日历公司统一。"""
    return run_kpi_hz_progress(connection, params)


def run_trend_sx_daily(connection, params) -> dict:
    """绍兴日销趋势（line）：trend_region_daily 的绍兴锚定版（月=锚点月，
    gran 透传缺省日）。"""
    anchor = _hz_anchor(params)
    return run_trend_region_daily(
        connection,
        _carry_scope(params, {
            "region": _SX_REGION,
            "month": anchor.strftime("%Y-%m"),
            "gran": params.get("gran") or "day",
        }),
    )


def run_bar_sx_dept(connection, params) -> dict:
    """绍兴部门本月排行（bar）：bar_department_mtd 的绍兴锚定版。"""
    anchor = _hz_anchor(params)
    return run_bar_department_mtd(
        connection,
        _carry_scope(params, {"region": _SX_REGION, "month": anchor.strftime("%Y-%m")}),
    )


def run_table_sx_people_progress(connection, params) -> dict:
    """绍兴个人完成率榜（table）：``_people_progress_payload`` 绍兴锚定版。"""
    return _people_progress_payload(
        connection, _SX_REGION, _hz_anchor(params), scope_from_params(params)
    )


def run_table_sx_unfilled_today(connection, params) -> dict:
    """绍兴当日未填报人（table）：``_unfilled_payload`` 绍兴锚定版。"""
    return _unfilled_payload(
        connection, _SX_REGION, _hz_anchor(params), scope_from_params(params)
    )


_OA_TODAY_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) AS `s` "
    "FROM `fact_daily_report_offline` "
    "WHERE `business_date` = %s "
    "AND `responsible_person` NOT LIKE '%%合计%%' "
    + _OFFLINE_LINE_SQL
)


def run_kpi_oa_mtd(connection, params) -> dict:
    """线下整体本月累计 + 达成率（scalar）：kpi_region_mtd 全域锚定版
    （月 = 锚点日所在月，region=None = 线下线全域）。

    行级 scope（M3）：``offline_all`` 授权经 :func:`_oa_effective_scope`
    展开为三区并集（B5 聚合授权键），其余区域授权语义不变。
    """
    anchor = _hz_anchor(params)
    return run_kpi_region_mtd(
        connection,
        _carry_oa_scope(params, {"month": anchor.strftime("%Y-%m")}),
    )


def run_kpi_oa_today(connection, params) -> dict:
    """线下整体当日销售（scalar）：锚点日 Σ sales_amount（线下线全域 +
    排合计行）。

    行级 scope（M3）：语句已参数化，scope 非 None 时末尾追加
    ``region IN`` 片段——汇总口径随之收窄到可见区域（fail-closed）；
    ``offline_all`` 授权先经 :func:`_oa_effective_scope` 展开为三区并集。
    """
    anchor = _hz_anchor(params)
    fragment, scope_params = _scope_region_filter(
        _oa_effective_scope(scope_from_params(params))
    )
    rows = _fetch_rows(
        connection, _append_region_sql(_OA_TODAY_SQL, fragment), (anchor,) + scope_params
    )
    value = rows[0]["s"] if rows else 0
    return {
        "chart": "scalar",
        "value": float(value),
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_kpi_oa_progress(connection, params) -> dict:
    """线下整体时间进度（scalar）：与杭州卡同函数——工作日日历公司统一
    （dim_calendar 不分区域），已过工作日 ÷ 全月工作日，含锚点当天。"""
    return run_kpi_hz_progress(connection, params)


def run_trend_oa_daily(connection, params) -> dict:
    """线下整体区域日销趋势（line）：trend_region_daily 全域锚定版
    （分区域多系列；月 = 锚点月，gran 透传缺省日）。

    行级 scope（M3）：``offline_all`` 经 :func:`_oa_effective_scope`
    展开为三区并集（B5 聚合授权键）。
    """
    anchor = _hz_anchor(params)
    return run_trend_region_daily(
        connection,
        _carry_oa_scope(params, {
            "month": anchor.strftime("%Y-%m"),
            "gran": params.get("gran") or "day",
        }),
    )


_OA_MERGED_REGIONS = ("hangzhou", "shaoxing", "offline_extra")


def _oa_effective_scope(scope):
    """oa 卡专用 scope 展开（B5，2026-10-15 用户裁决）。

    ``offline_all`` 是「线下整体」**聚合授权键**：事实表本无该区行
    （oa = 杭/绍/省外三区并集），直接下推 SQL 只会得空——语义错位。
    授权 ``offline_all`` 在此展开为 ``_OA_MERGED_REGIONS`` 并集；其余
    键（hangzhou/shaoxing/offline_extra 单独授权）原样保留。
    ``None`` 或不含 ``offline_all`` → 原样返回（旧路径零变化）。
    """
    if scope is None or "offline_all" not in scope:
        return scope
    return (scope - {"offline_all"}) | frozenset(_OA_MERGED_REGIONS)


def _carry_oa_scope(params, target):
    """oa 委派卡（mtd/trend）专用：先展开 ``offline_all`` 再转抄指纹。

    与 :func:`_carry_scope` 的差别仅在展开——hz/sx 锚定卡钉死单一真实
    区域，不适用聚合键语义，仍用原函数。
    """
    fingerprint = scope_fingerprint(_oa_effective_scope(scope_from_params(params)))
    if fingerprint is not None:
        target[ROW_SCOPE_PARAM] = fingerprint
    return target


def _oa_people(connection, anchor, scope=None):
    """线下整体人员榜采集（BI 实时口径 include_today=True）。

    fail-soft：dim_calendar 缺行（MartTaskError）→ 空人空日历，卡片
    降级空态而非 5xx（同 hz 人员卡纪律）。

    行级 scope（M3）：``collect_offline_all_people`` 是共享采集层，合并
    结果无法按区域再切——**不能**只凭交集放行（少授一个区域就多暴露
    该区人员），故 scope 非 None 时要求三区全在 scope 内，否则整表空；
    ``offline_all`` 授权先经 :func:`_oa_effective_scope` 展开为三区并集。
    """
    from common.daily_robot.offline_summary import collect_offline_all_people
    if not _scope_allows_regions(_oa_effective_scope(scope), *_OA_MERGED_REGIONS):
        return [], frozenset()
    try:
        return collect_offline_all_people(
            connection, business_date=anchor, include_today=True
        )
    except MartTaskError:
        return [], frozenset()


def _dept_progress_from_people(people, progress):
    """人员行（归位后 dept）→ 部门进度聚合（2026-10-17 部门主视角裁决）。

    部门月目标 = 成员月目标加总（同「MAX 后求和」纪律的聚合形态——people
    行已是逐人单值）；达成率 = Σ完成/Σ目标（无目标 → None，沉底不参与
    达成率排序，与人员榜 rate=None 同语义）；进度差 = 达成率 − 工作日
    进度基准。排序：有目标按达成率降序，无目标沉底按完成额降序。
    """
    groups = {}
    for person in people:
        dept = person.get("dept") or "未分组"
        bucket = groups.setdefault(dept, {"count": 0, "completed": 0.0,
                                          "target": 0.0})
        bucket["count"] += 1
        bucket["completed"] += float(person.get("completed") or 0.0)
        bucket["target"] += float(person.get("target") or 0.0)
    rows = []
    for dept, bucket in groups.items():
        target = bucket["target"]
        completed = bucket["completed"]
        rate = (completed / target) if target > 0 else None
        rows.append({
            "dept": dept,
            "count": bucket["count"],
            "completed": completed,
            "target": target,
            "rate": rate,
            "diff": (rate - progress) if rate is not None else None,
        })
    rows.sort(key=lambda r: (r["rate"] is None, -(r["rate"] or 0.0),
                             -r["completed"]))
    return [
        {"rank": index, **row} for index, row in enumerate(rows, start=1)
    ]


_DEPT_PROGRESS_COLUMNS = [
    {"key": "rank", "title": "排名"},
    {"key": "dept", "title": "部门"},
    {"key": "count", "title": "人数"},
    {"key": "completed", "title": "月累计", "format": "wan"},
    {"key": "target", "title": "月目标", "format": "wan"},
    {"key": "rate", "title": "达成率", "format": "percent"},
    {"key": "diff", "title": "进度差", "format": "percent"},
]


def _region_dept_progress(connection, region, anchor, scope=None):
    """hz/sx 部门进度卡载荷：人员榜同源采集 + 通讯录归位部门后聚合。

    部门归属以通讯录为准（fetch_member_dept_map，无匹配保留表内值——
    2026-09-23「人与部门不要混乱」裁决的部门口径），与 oa 部门卡一致。
    """
    if not _scope_allows_regions(scope, region):
        data = None
    else:
        try:
            data = mart_collect(
                connection, region=region, business_date=anchor,
                include_today=True,
            )
        except MartTaskError:
            data = None
    rows = []
    if data is not None and data.workdays:
        progress = len(data.elapsed) / len(data.workdays)
        from common.daily_robot.offline_summary import fetch_member_dept_map
        dept_map = fetch_member_dept_map(connection, (region,))
        people = []
        for person in data.people:
            people.append({
                "dept": dept_map.get((region, person["name"]),
                                     person["dept"] or "未分组"),
                "completed": person["completed"],
                "target": person["target"],
            })
        rows = _dept_progress_from_people(people, progress)
    return {
        "chart": "table",
        "columns": list(_DEPT_PROGRESS_COLUMNS),
        "rows": rows,
        "date": anchor.isoformat(),
    }


def run_table_hz_dept_progress(connection, params) -> dict:
    """杭州部门进度表（table）：部门×人数×月累计/月目标/达成率/进度差。"""
    anchor = _hz_anchor(params)
    return _region_dept_progress(connection, "hangzhou", anchor,
                                 scope_from_params(params))


def run_table_sx_dept_progress(connection, params) -> dict:
    """绍兴部门进度表（table）：同杭州形态（日期锚点与 hz 共用 _hz_anchor）。"""
    anchor = _hz_anchor(params)
    return _region_dept_progress(connection, "shaoxing", anchor,
                                 scope_from_params(params))


def run_bar_oa_dept(connection, params) -> dict:
    """线下整体部门本月排行（bar）：按部门 Σ完成额 降序。

    真源 = ``collect_offline_all_people``（杭/绍部门经通讯录归位、
    offline_extra 板块归位，与 08:30 播报部门排名同口径）——不用裸
    ``department`` 列：省外板块事实行的部门列是责任人名，直读会人/
    部门混杂（运维 2026-09-23「人与部门不要混乱」裁决）。
    """
    anchor = _hz_anchor(params)
    people, _workdays = _oa_people(connection, anchor, scope_from_params(params))
    totals = {}
    for person in people:
        dept = person["dept"] or "未分组"
        totals[dept] = totals.get(dept, 0.0) + float(person["completed"])
    ranking = sorted(totals.items(), key=lambda item: -item[1])
    return {
        "chart": "bar",
        "categories": [dept for dept, _ in ranking],
        "values": [value for _, value in ranking],
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_table_oa_people_progress(connection, params) -> dict:
    """线下整体个人完成率榜（table）：静态榜单页人员总榜同口径全列。

    排名/姓名/部门/完成/月目标/达成率/进度差/日均完成/预计月末/未填；
    进度基准 = 已过工作日 ÷ 全月工作日（含锚点当天，BI 实时口径）。
    dim_calendar 缺行降级空表。
    """
    anchor = _hz_anchor(params)
    people, workdays = _oa_people(connection, anchor, scope_from_params(params))
    rows = []
    if workdays:
        elapsed = elapsed_workdays(workdays, today=anchor, include_today=True)
        n_elapsed = len(elapsed)
        progress = n_elapsed / len(workdays)
        for index, person in enumerate(people, start=1):
            rate = person["rate"]
            rows.append({
                "rank": index,
                "name": person["name"],
                "dept": person["dept"],
                "completed": float(person["completed"]),
                "target": float(person["target"]),
                "rate": rate,
                "diff": (rate - progress) if rate is not None else None,
                "daily": (float(person["completed"]) / n_elapsed
                          if n_elapsed else 0.0),
                "proj": (rate / progress
                         if rate is not None and progress > 0 else None),
                "unfilled": person["unfilled"],
            })
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "排名"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
            {"key": "completed", "title": "完成额", "format": "wan"},
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "rate", "title": "达成率", "format": "percent"},
            {"key": "diff", "title": "进度差", "format": "percent"},
            {"key": "daily", "title": "日均完成", "format": "wan"},
            {"key": "proj", "title": "预计月末", "format": "percent"},
            {"key": "unfilled", "title": "未填"},
        ],
        "rows": rows,
        "date": anchor.isoformat(),
    }


def run_table_oa_dept_progress(connection, params) -> dict:
    """线下整体部门进度表（table，2026-10-17 部门主视角裁决）。

    部门 = 归位后口径（杭/绍通讯录归位、补集表内值兜底——同
    ``run_bar_oa_dept`` 真源）；部门月目标=成员目标加总；无目标部门
    达成率 None 沉底。行级 scope 与人员榜同门（fail-closed）。
    """
    anchor = _hz_anchor(params)
    people, workdays = _oa_people(connection, anchor, scope_from_params(params))
    rows = []
    if workdays:
        progress = len(elapsed_workdays(workdays, today=anchor,
                                        include_today=True)) / len(workdays)
        rows = _dept_progress_from_people(people, progress)
    return {
        "chart": "table",
        "columns": list(_DEPT_PROGRESS_COLUMNS),
        "rows": rows,
        "date": anchor.isoformat(),
    }


_OA_SCOPE_COLUMNS = [
    {"key": "scope", "title": "板块"},
    {"key": "day", "title": "当日", "format": "wan"},
    {"key": "day_base", "title": "昨日", "format": "wan"},
    {"key": "dod", "title": "日环比", "format": "percent"},
    {"key": "week", "title": "本周累计", "format": "wan", "cls": "col-sep"},
    {"key": "week_base", "title": "上周累计", "format": "wan"},
    {"key": "wow", "title": "周环比", "format": "percent"},
    {"key": "completed", "title": "月累计", "format": "wan", "cls": "col-sep"},
    {"key": "month_base", "title": "上月累计", "format": "wan"},
    {"key": "mom", "title": "月环比", "format": "percent"},
    {"key": "target", "title": "月目标", "format": "wan", "cls": "col-sep"},
    {"key": "rate", "title": "达成率", "format": "percent"},
    {"key": "prev_target", "title": "上月目标", "format": "wan"},
    {"key": "prev_rate", "title": "上月达成率", "format": "percent"},
]


def run_table_oa_scopes(connection, params) -> dict:
    """线下整体板块日/周/月总览（table）：静态页面板同口径。

    真源 = ``offline_summary.AGG_SCOPES`` + ``compute_overview_row``
    （杭州/绍兴/其他（补集桶） + 线下整体，与 20:30 汇总播报、
    静态页面板同函数；2026-10-16 三桶裁决：省外/总经办/李树军
    拆分退役）。报告日 = 锚点前（含）最近一个全板块合计非零
    自然日；周/月列锚定锚点日（BI 实时口径含当天，静态页 T-1 刻意
    不同）。本月全板块零填报 → 空行降级。
    """
    from common.daily_robot.offline_summary import (
        AGG_SCOPES,
        TOTAL_LABEL,
        TOTAL_SCOPE_KEY,
        compute_overview_row,
        fetch_scope_daily_facts,
        fetch_scope_month_target,
        merge_facts,
        merge_targets,
        previous_month,
    )
    from common.metrics.daily_report import achievement_rate

    anchor = _hz_anchor(params)
    if not _scope_allows_regions(
        _oa_effective_scope(scope_from_params(params)), *_OA_MERGED_REGIONS
    ):
        # 行级 scope（M3）：板块总览逐板块取数后 merge（且含「李树军」这类
        # 非 region 口径板块），无法按区域下推——同 _oa_people：三区不全
        # 在 scope 内即整表空（fail-closed，绝不用全域板块冒充可见）。
        return {
            "chart": "table",
            "columns": _OA_SCOPE_COLUMNS,
            "rows": [],
            "date": anchor.isoformat(),
        }
    prev_first, _ = previous_month(anchor)
    facts_by_scope = {}
    targets = {}
    prev_targets = {}
    for scope, label, region, scope_anchor in AGG_SCOPES:
        facts_by_scope[scope] = fetch_scope_daily_facts(
            connection, region=region, anchor=scope_anchor,
            start=prev_first, end=anchor,
        )
        targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=scope_anchor,
            year=anchor.year, month=anchor.month,
        )
        # 上月目标（运维 2026-10-10 排版裁决新增「上月目标/上月达成率」）
        prev_targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=scope_anchor,
            year=prev_first.year, month=prev_first.month,
        )

    day_totals = {}
    for facts in facts_by_scope.values():
        for day, amount in facts.items():
            if day <= anchor:
                day_totals[day] = day_totals.get(day, 0.0) + amount
    filled = sorted(d for d, total in day_totals.items() if total != 0)
    if not filled:
        return {
            "chart": "table",
            "columns": _OA_SCOPE_COLUMNS,
            "rows": [],
            "date": anchor.isoformat(),
        }
    report_day = filled[-1]

    metrics = [
        compute_overview_row(
            scope, label, facts=facts_by_scope[scope],
            report_day=report_day, data_date=anchor,
            month_target=targets[scope],
        )
        for scope, label, _, _ in AGG_SCOPES
    ]
    metrics.append(compute_overview_row(
        TOTAL_SCOPE_KEY, TOTAL_LABEL,
        facts=merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES]),
        report_day=report_day, data_date=anchor,
        month_target=merge_targets(targets.values()),
    ))

    scope_prev_target = {
        **prev_targets,
        TOTAL_SCOPE_KEY: merge_targets(prev_targets.values()),
    }
    rows = [
        {
            "scope": m.label,
            "day": m.day_sales,
            "day_base": m.day_base,
            "dod": m.dod_rate,
            "week": m.week_sales,
            "week_base": m.week_base,
            "wow": m.wow_rate,
            "completed": m.month_completed,
            "month_base": m.month_base,
            "mom": m.mom_rate,
            "target": m.month_target,
            "rate": m.month_rate,
            "prev_target": scope_prev_target[m.scope],
            # 上月达成率 = 上月同期累计 ÷ 上月目标（与本月达成率同构）
            "prev_rate": achievement_rate(
                m.month_base, scope_prev_target[m.scope]
            ),
        }
        for m in metrics
    ]
    return {
        "chart": "table",
        "columns": _OA_SCOPE_COLUMNS,
        "rows": rows,
        "date": anchor.isoformat(),
        "report_day": report_day.isoformat(),
    }

__all__ = ('_HZ_REGION', '_WORKDAY_OPTIONS_SQL', '_HZ_TODAY_SQL', 'workday_options', '_hz_anchor', 'run_kpi_hz_mtd', 'run_kpi_hz_today', 'run_kpi_hz_progress', 'run_bar_hz_dept', 'run_trend_hz_daily', '_unfilled_payload', 'run_table_hz_unfilled_today', '_people_progress_payload', 'run_table_hz_people_progress', '_SX_REGION', '_SX_TODAY_SQL', 'run_kpi_sx_mtd', 'run_kpi_sx_today', 'run_kpi_sx_progress', 'run_trend_sx_daily', 'run_bar_sx_dept', 'run_table_sx_people_progress', 'run_table_sx_unfilled_today', '_OA_TODAY_SQL', 'run_kpi_oa_mtd', 'run_kpi_oa_today', 'run_kpi_oa_progress', 'run_trend_oa_daily', '_OA_MERGED_REGIONS', '_oa_effective_scope', '_carry_oa_scope', '_oa_people', '_dept_progress_from_people', '_DEPT_PROGRESS_COLUMNS', '_region_dept_progress', 'run_table_hz_dept_progress', 'run_table_sx_dept_progress', 'run_bar_oa_dept', 'run_table_oa_people_progress', 'run_table_oa_dept_progress', '_OA_SCOPE_COLUMNS', 'run_table_oa_scopes')
