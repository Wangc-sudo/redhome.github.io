"""BI offline 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_OFFLINE_LINE_SQL = "AND region NOT IN ('电商', 'qudao', 'vanke')"


_OFFLINE_MTD_SQL = textwrap.dedent(
    f"""
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')
    AND business_date <= %s
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_OFFLINE_WINDOW_SQL = textwrap.dedent(
    f"""
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_OFFLINE_YTD_SQL = textwrap.dedent(
    f"""
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1)
    AND business_date <= %s
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_OFFLINE_ANNUAL_SQL = textwrap.dedent(
    f"""
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1)
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_REGION_DAILY_SQL = textwrap.dedent(
    f"""
    SELECT business_date, region, SUM(sales_amount) AS total
    FROM fact_daily_report_offline
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    {_OFFLINE_LINE_SQL}
    GROUP BY business_date, region
    """
).strip()


def _offline_mtd_sum(connection, latest, scope=None) -> Decimal:
    """当月 1 号 → 水位日的 Σsales；水位为空（空表）时直接 0，不发起求和。

    行级 scope（M3）：scope 非 None 时在窗口 SQL 末尾追加参数化
    ``region IN``；scope=None 时语句与参数逐字节不变。
    """
    if latest is None:
        return Decimal(0)
    fragment, scope_params = _scope_region_filter(scope)
    sql = _append_region_sql(_OFFLINE_MTD_SQL, fragment)
    return _fetch_scalar(connection, sql, (latest,) + scope_params)


def offline_window_sum(connection, *, first_day, last_day, scope=None) -> Decimal:
    """线下 [first_day, last_day] 闭区间 Σsales（合计行 + 电商线排除）。

    粒度批次 A3 的共用窗口核：调用方负责按档位算边界（周=周一→锚点、
    日=锚点当日、月=月首→锚点），本函数不掺任何时钟。行级 scope（M3）
    与 :func:`_offline_mtd_sum` 同款下推。
    """
    fragment, scope_params = _scope_region_filter(scope)
    sql = _append_region_sql(_OFFLINE_WINDOW_SQL, fragment)
    return _fetch_scalar(
        connection, sql, (first_day, last_day) + scope_params
    )


def offline_mtd_total(connection) -> Decimal:
    """线下本月累计销售 (①)：窗口上界 = 真实数据水位（非 CURDATE()）。

    日报 T+1 未入仓时「本月累计」截至最新入仓日；合计行排除不变；
    水位滞后整月时区间为空、和为 0。B3 起排除 qudao/vanke/电商（归
    电商/体验馆线，名单见 ``_OFFLINE_LINE_SQL``），水位同源（同为线下口径）。
    """
    return _offline_mtd_sum(connection, offline_latest_date(connection))


def offline_annual_total(connection) -> Decimal:
    """线下年累计: year-to-date offline sales, same 合计 exclusion."""
    return _fetch_scalar(connection, _OFFLINE_ANNUAL_SQL)


def region_daily_series(connection, scope=None) -> dict:
    """L1 无参路径：当前月全区域日序列（窗口 SQL 全静态）。

    轴由「有数据的日」改为 月首→min(月末, 锚点) 的完整日窗口（三态：
    缺失日 null 断线、营业日真实 0 标 zero）；B2 起锚点 = 真实数据
    水位（多一条静态 MAX 定锚查询），T+1 未入仓的日子不再进轴。

    行级 scope（M3）：源 SQL 已按 ``business_date × region`` 分组，逐行
    丢弃不在 scope 内的 region 与下推 ``region IN`` 结果逐字节等价——
    故本卡走 Python 过滤，保住「无参静态 SQL」契约（StaticSqlTests 与
    FakeConnection 断言 ``executed`` 里无 ``%s``）；scope=空集 ⇒ 无行
    （fail-closed，轴仍在但全 missing，不声称有数）。
    """
    today = datetime.now().date()
    first_day, last_day = month_bounds(today.strftime("%Y-%m"))
    anchor = _offline_trend_anchor(connection, first_day, last_day)
    rows = _fetch_rows(connection, _REGION_DAILY_SQL)
    if scope is not None:
        rows = [row for row in rows if row.get("region") in scope]
    return _daily_three_state(
        connection, rows, "region", first_day, last_day, anchor=anchor
    )


def region_mtd_total(connection, *, region=None, first_day, last_day,
                     scope=None) -> Decimal:
    """选中区域（缺省全区域）某自然月 Σsales（截断未来 + 排除合计行）。

    B3 起「全区域」= 线下线全域（排除 qudao/vanke/电商，见
    ``_OFFLINE_LINE_SQL``）；显式选名单内区域（如 qudao）得空集——
    归电商/体验馆线，本查询族不服务。带参 SQL 的字面 ``%`` 双写（pymysql
    ``sql % params`` 约定）：``'%%合计%%'`` 格式化后 MySQL 收到
    ``'%合计%'``。
    """
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT COALESCE(SUM(sales_amount), 0)
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        """
    ).strip()
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def region_month_target(connection, *, region=None, first_day, last_day,
                        scope=None) -> Decimal:
    """区域月目标 = Σ各人员 MAX(monthly_target)（melt 陷阱：绝不跨行 SUM）。

    与 ``summarize_people`` 同口径取**全月行** MAX：月目标是月级常量，
    预填未来行不影响 MAX，故不做 CURDATE 截断（与取数查询刻意不对称）。
    """
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT COALESCE(SUM(mx), 0)
        FROM (
        SELECT responsible_person, MAX(monthly_target) AS mx
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        GROUP BY responsible_person
        ) t
        """
    ).strip()
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def department_mtd_ranking(connection, *, region=None, first_day, last_day,
                           scope=None) -> dict:
    """选中区域（缺省全区域）某自然月按部门 Σsales 降序；空部门→未分组。

    2026-10-19 归位升级（用户裁决，画布与 BI 同真源）：人级聚合后经
    归位口径（``fetch_member_dept_map``，含 dim_org_assignment_override
    覆盖层）映射部门再聚合——画布拉线归口对部门排行同步生效
    （杭州销售部案例：父部门直属者被叶子部门先认领，纯 SQL GROUP BY
    department 永远看不到父级）；映射无命中回退表内 department 值
    （既有纪律不变），归位读取失败 fail-soft 同纯表内口径。
    """
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT responsible_person, region, department,
               SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        GROUP BY responsible_person, region, department
        """
    ).strip()
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    dept_map = {}
    row_regions = tuple({row["region"] for row in rows if row.get("region")})
    if row_regions:
        from common.daily_robot.offline_summary import fetch_member_dept_map
        dept_map = fetch_member_dept_map(connection, row_regions)
    bucket = {}
    for row in rows:
        dept = (
            dept_map.get((row.get("region"), row["responsible_person"]))
            or row["department"] or "未分组"
        )
        bucket[dept] = bucket.get(dept, Decimal(0)) + Decimal(row["total"] or 0)
    ordered = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "categories": [dept for dept, _ in ordered],
        "values": [total for _, total in ordered],
    }


def region_month_daily_series(connection, *, region=None, first_day, last_day,
                              scope=None) -> dict:
    """参数化的区域日销序列（L2 用）；有 region 单系列、无则全区域系列。

    L1 无参路径仍走静态 ``region_daily_series``（StaticSqlTests 锁定）；
    行级 scope（M3）与显式 region 同时生效：``region = %s AND region IN``
    交集，钉死区域不在 scope 内即空集（fail-closed）。
    """
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT business_date, region, SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        GROUP BY business_date, region
        """
    ).strip()
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return _daily_three_state(
        connection, rows, "region", first_day, last_day,
        anchor=_offline_trend_anchor(connection, first_day, last_day),
    )


def region_weekly_series(connection, *, region=None, month, scope=None) -> dict:
    """周档位区域序列：month=窗口截止月，锚点所在自然周起向前 12 周。

    周一为首、跨月周归属起始日所在月；桶内求和由 SQL 下推（
    ``DATE_SUB(business_date, INTERVAL WEEKDAY(business_date) DAY)``
    = 周一），值一律 %s 绑定。三态判定与日档同一核心（桶 ∩ 锚点内
    有营业日才允许 zero）。

    B1：分桶表达式别名 ``bucket_start`` 刻意不与真实列同名——别名
    撞名时 MySQL 的 GROUP BY 会解析回真实 ``business_date`` 列，桶内
    按天散行、配合旧的覆盖赋值只留最后一天（P0 假数）；Python 侧
    同键多行累加兜底（:func:`_align_series_rows`）。B2：锚点改真实
    数据水位（:func:`_offline_trend_anchor`）。
    """
    first_day, last_day = month_bounds(month)
    as_of = _offline_trend_anchor(connection, first_day, last_day)
    axis = _week_axis(as_of) if as_of is not None else []
    workdays = _window_workdays(connection, axis[0] if axis else first_day, last_day)
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT DATE_SUB(business_date, INTERVAL WEEKDAY(business_date) DAY) AS bucket_start,
               region, SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        GROUP BY bucket_start, region
        """
    ).strip()
    rows = _fetch_rows(
        connection, sql, (axis[0] if axis else first_day, last_day) + region_params
    )
    aligned = _align_series_rows(
        rows, "region", axis=axis, workdays=workdays, as_of=as_of,
        bucket="week", date_key="bucket_start",
    )
    aligned["as_of"] = as_of.isoformat() if as_of is not None else None
    return aligned


def region_monthly_series(connection, *, region=None, month, scope=None) -> dict:
    """月档位区域序列：month=窗口截止月，向前 12 个自然月（YYYY-MM 轴）。

    桶键 = 月初日（SQL 下推 ``DAYOFMONTH`` 归一，别名 ``bucket_start``
    刻意不与真实列同名，B1 同周档），标签 ``%Y-%m``；值一律 %s 绑定。
    三态判定与日档同一核心；B2 起锚点 = 真实数据水位。
    """
    first_day, last_day = month_bounds(month)
    as_of = _offline_trend_anchor(connection, first_day, last_day)
    axis = _month_axis(first_day) if as_of is not None else []
    workdays = _window_workdays(connection, axis[0] if axis else first_day, last_day)
    region_sql, region_params = _scope_region_filter(scope, region)
    sql = textwrap.dedent(
        f"""
        SELECT DATE_SUB(business_date, INTERVAL DAYOFMONTH(business_date) - 1 DAY) AS bucket_start,
               region, SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        {_OFFLINE_LINE_SQL}
        GROUP BY bucket_start, region
        """
    ).strip()
    rows = _fetch_rows(
        connection, sql, (axis[0] if axis else first_day, last_day) + region_params
    )
    aligned = _align_series_rows(
        rows, "region", axis=axis, workdays=workdays, as_of=as_of,
        label_fmt="%Y-%m", bucket="month", date_key="bucket_start",
    )
    aligned["as_of"] = as_of.isoformat() if as_of is not None else None
    return aligned


_OFFLINE_DOD_LATEST_SQL = textwrap.dedent(
    f"""
    SELECT MAX(business_date) AS d
    FROM fact_daily_report_offline
    WHERE business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_OFFLINE_DOD_LATEST_SCOPED_SQL = textwrap.dedent(
    f"""
    SELECT MAX(business_date) AS d
    FROM fact_daily_report_offline
    WHERE business_date <= CURDATE()
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    """
).strip()


_OFFLINE_DOD_WINDOW_SQL = textwrap.dedent(
    f"""
    SELECT business_date, SUM(sales_amount) AS total
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    GROUP BY business_date
    """
).strip()


def offline_latest_date(connection, scope=None):
    """线下日报数据水位：最新入仓 business_date（空表 None）。

    与 :func:`offline_dod` 共用定锚语句（含未来行截断 + 合计行排除）；
    B2 起 MTD 窗口上界、线下趋势卡锚点与日环比同源。行级 scope（M3）：
    scope 非 None 时改走参数化孪生语句，水位也按可见区域取——否则
    「一区域全空」的授权仍会从全域水位里读出别人区域的最新日。
    """
    if scope is None:
        return _fetch_one(connection, _OFFLINE_DOD_LATEST_SQL)
    fragment, scope_params = _scope_region_filter(scope)
    sql = _append_region_sql(_OFFLINE_DOD_LATEST_SCOPED_SQL, fragment)
    return _fetch_one(connection, sql, scope_params)


def offline_dod(connection, scope=None):
    """⑭ 线下日环比核：最新数据日 Σsales、前一自然日、近 7 自然日。"""
    latest = offline_latest_date(connection, scope)
    if latest is None:
        return None
    return _dod_core(
        _dod_totals(connection, _OFFLINE_DOD_WINDOW_SQL, latest, scope), latest
    )


def run_kpi_offline_mtd(connection, params) -> dict:
    """①线下本月累计销售: scalar KPI, 元（粒度批次 A：日/周/月三档，默认月）。

    档位语义（``month`` 参数为窗口月，缺省 = 水位所在月）：

    * ``month``  该月 1 号 → 锚点的区间累计——缺省路径（无 month）复用
      ``_offline_mtd_sum``，与 B2 版载荷逐字节一致；
    * ``week``   锚点所在自然周（周一为首）→ 锚点的区间累计；
    * ``day``    锚点当日值；
    * ``year``   年首（MySQL 时钟）→ 锚点的年度累计（A5；month 参数
      可把年累计截到该月末，锚点规则不变）。

    锚点 = min(窗口月末, 数据水位)；水位 None（空表）或锚点落在窗口月
    之前 ⇒ value=0、as_of=None（该月无入仓，不假装有数）。``as_of``
    恒为数字的真实截止日，前端标量卡角标/页脚直接消费
    （``payload.date || payload.as_of``）。边界日期全部来自 DB 侧
    （水位 / param month / SQL 内 CURDATE 月首），本卡不混用 Python
    时钟（B6）；gran 合法性由 app 层 granularity 值域闸保证。
    """
    month = (params.get("month") or "").strip()
    gran = (params.get("gran") or "month").strip()
    scope = scope_from_params(params)
    latest = offline_latest_date(connection, scope)
    value, as_of = Decimal(0), None
    if latest is not None:
        if month:
            first_day, last_day = month_bounds(month)
            anchor = min(last_day, latest)
            if anchor < first_day:
                anchor = None  # 窗口月整体在水位之后：该月无入仓
        else:
            first_day, anchor = None, latest
        if anchor is not None:
            if gran == "day":
                value = offline_window_sum(
                    connection, first_day=anchor, last_day=anchor, scope=scope
                )
            elif gran == "week":
                monday = anchor - timedelta(days=anchor.weekday())
                value = offline_window_sum(
                    connection, first_day=monday, last_day=anchor, scope=scope
                )
            elif gran == "year":
                fragment, scope_params = _scope_region_filter(scope)
                value = _fetch_scalar(
                    connection,
                    _append_region_sql(_OFFLINE_YTD_SQL, fragment),
                    (anchor,) + scope_params,
                )
            elif first_day is not None:
                value = offline_window_sum(
                    connection, first_day=first_day, last_day=anchor, scope=scope
                )
            else:
                value = _offline_mtd_sum(connection, latest, scope)
            as_of = anchor
    return {
        "chart": "scalar",
        "value": float(value),
        "as_of": as_of.isoformat() if as_of is not None else None,
        "unit": _UNIT,
    }


_REGION_LABEL_CACHE = {"at": 0.0, "map": {}}


_REGION_LABEL_TTL_SECONDS = 300.0


def _region_label_map(connection) -> dict:
    import time

    now = time.monotonic()
    cache = _REGION_LABEL_CACHE
    if cache["map"] and now - cache["at"] < _REGION_LABEL_TTL_SECONDS:
        return cache["map"]
    mapping = {}
    try:
        rows = _fetch_rows(
            connection,
            "SELECT `region_key` AS `k`, `label` AS `v` "
            "FROM `dim_org_region` WHERE `label` IS NOT NULL AND `label` <> ''",
        )
        mapping = {row["k"]: row["v"] for row in rows}
    except Exception:  # 目录表未就绪/抖动 → 回退原键
        mapping = {}
    cache["at"] = now
    cache["map"] = mapping
    return mapping


def _region_label(connection, region_key):
    """region 键 → 中文名（无映射回退原键）。"""
    return _region_label_map(connection).get(region_key) or region_key


def run_trend_region_daily(connection, params) -> dict:
    """区域销售趋势: gran=day 月内日序列（缺省）/ week 12 周 / month 12 月。

    L1 无参=当前月全区域（静态 SQL）；带参=region/month/gran。gran 未传
    由 app 层注入 default_gran（"day"），故 gran=day 与不带参走同一
    路径、响应逐字节一致。``status`` 与 ``series`` 一一对应、与
    ``dates`` 等长；``as_of`` 为事实表数据水位。
    """
    region = params.get("region")
    month = params.get("month")
    gran = params.get("gran") or "day"
    scope = scope_from_params(params)
    if gran == "week":
        series = region_weekly_series(
            connection, region=region,
            month=month or datetime.now().strftime("%Y-%m"), scope=scope,
        )
    elif gran == "month":
        series = region_monthly_series(
            connection, region=region,
            month=month or datetime.now().strftime("%Y-%m"), scope=scope,
        )
    elif region is None and month is None:
        series = region_daily_series(connection, scope)
    else:
        month = month or datetime.now().strftime("%Y-%m")
        first_day, last_day = month_bounds(month)
        series = region_month_daily_series(
            connection, region=region, first_day=first_day, last_day=last_day,
            scope=scope,
        )
    return {
        "chart": "line",
        "dates": series["dates"],
        "series": [
            {"name": _region_label(connection, entry["name"]),
             "data": [None if value is None else float(value)
                      for value in entry["data"]]}
            for entry in series["series"]
        ],
        "status": series["status"],
        "as_of": series["as_of"],
    }


def run_kpi_region_mtd(connection, params) -> dict:
    """区域当月累计（模块 ① 区域下钻）: region/month 参数化 scalar + 进度条。

    value=当月 Σsales（截断+排合计）；target=Σ各人员 MAX(monthly_target)
    （melt 安全）；rate=value/target（target 0→None）。region 缺省全区域，
    month 缺省当前月（应用本地时钟；容器 TZ Asia/Shanghai）。
    """
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    scope = scope_from_params(params)
    first_day, last_day = month_bounds(month)
    value = region_mtd_total(
        connection, region=region, first_day=first_day, last_day=last_day,
        scope=scope,
    )
    target = region_month_target(
        connection, region=region, first_day=first_day, last_day=last_day,
        scope=scope,
    )
    rate = None if target == 0 else float(value) / float(target)
    return {
        "chart": "scalar",
        "value": float(value),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_bar_department_mtd(connection, params) -> dict:
    """区域部门当月排行（模块 ① 区域下钻）: region/month bar 降序。"""
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    scope = scope_from_params(params)
    first_day, last_day = month_bounds(month)
    ranking = department_mtd_ranking(
        connection, region=region, first_day=first_day, last_day=last_day,
        scope=scope,
    )
    return {
        "chart": "bar",
        "categories": ranking["categories"],
        "values": [float(value) for value in ranking["values"]],
        "unit": _UNIT,
    }


def run_kpi_offline_dod(connection, params):
    """⑭ 线下日环比卡（L1，无参）。"""
    return _run_dod(offline_dod(connection, scope_from_params(params)))


def _offline_trend_anchor(connection, first_day, last_day):
    """线下趋势卡锚点：当前月=真实数据水位，历史月=月末。

    与缺口口径 :func:`_shortfall_as_of`（当前月=今天）刻意分开：
    趋势图不应把 T+1 未入仓的日子画进轴、把「未入仓」标成
    missing（两者不要混用——缺口/所需日均仍按今天算）。空表返回
    None（轴为空、as_of 不下发）；水位滞后整月时锚在月首前一天
    （轴同样为空）。历史月锚点必然月末，不查水位，省一条 MAX。
    """
    today = datetime.now().date()
    if not (first_day <= today <= last_day):
        return last_day
    latest = offline_latest_date(connection)
    if latest is None:
        return None
    return max(min(latest, last_day), first_day - timedelta(days=1))

__all__ = ('_OFFLINE_LINE_SQL', '_OFFLINE_MTD_SQL', '_OFFLINE_WINDOW_SQL', '_OFFLINE_YTD_SQL', '_OFFLINE_ANNUAL_SQL', '_REGION_DAILY_SQL', '_offline_mtd_sum', 'offline_window_sum', 'offline_mtd_total', 'offline_annual_total', 'region_daily_series', 'region_mtd_total', 'region_month_target', 'department_mtd_ranking', 'region_month_daily_series', 'region_weekly_series', 'region_monthly_series', '_OFFLINE_DOD_LATEST_SQL', '_OFFLINE_DOD_LATEST_SCOPED_SQL', '_OFFLINE_DOD_WINDOW_SQL', 'offline_latest_date', 'offline_dod', 'run_kpi_offline_mtd', '_REGION_LABEL_CACHE', '_REGION_LABEL_TTL_SECONDS', '_region_label_map', '_region_label', 'run_trend_region_daily', 'run_kpi_region_mtd', 'run_bar_department_mtd', 'run_kpi_offline_dod', '_offline_trend_anchor')
