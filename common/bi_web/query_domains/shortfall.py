"""BI shortfall 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *
from common.bi_web.query_domains.offline import _OFFLINE_LINE_SQL


_SHORTFALL_GRAINS = ("people", "region")


_SHORTFALL_FRONT_GRAIN = {"people": "person", "region": "region"}


_DEFAULT_SHORTFALL_GRAIN = "people"


_SHORTFALL_PEOPLE_SQL = textwrap.dedent(
    """
    SELECT t.person AS name, MAX(t.department) AS dept,
    SUM(t.mx) AS target, COALESCE(SUM(d.done), 0) AS done,
    (MAX(d.person) IS NOT NULL) AS has_fact
    FROM (
    SELECT responsible_person AS person, MAX(department) AS department,
    MAX(monthly_target) AS mx
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    {region_sql}
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY responsible_person
    ) t
    LEFT JOIN (
    SELECT responsible_person AS person, SUM(sales_amount) AS done
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    {region_sql}
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY responsible_person
    ) d ON d.person <=> t.person
    GROUP BY t.person
    ORDER BY target DESC, t.person
    """
).strip()


_SHORTFALL_REGION_SQL = textwrap.dedent(
    f"""
    SELECT t.region AS name, SUM(t.mx) AS target,
    COALESCE(SUM(d.done), 0) AS done,
    (MAX(d.responsible_person) IS NOT NULL) AS has_fact
    FROM (
    SELECT region, responsible_person, MAX(monthly_target) AS mx
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    GROUP BY region, responsible_person
    ) t
    LEFT JOIN (
    SELECT region, responsible_person, SUM(sales_amount) AS done
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%%合计%%'
    {_OFFLINE_LINE_SQL}
    GROUP BY region, responsible_person
    ) d ON d.region <=> t.region
    AND d.responsible_person <=> t.responsible_person
    GROUP BY t.region
    ORDER BY target DESC, t.region
    """
).strip()


_ANOMALY_TOP_N = 10


def shortfall_facts(connection, *, grain="people", region=None,
                    first_day, last_day, scope=None) -> list:
    """只读事实：``[{name, target, done}]``（金额与 ``_unit`` 同为元）。

    **不含任何派生列** —— 派生一律走 :mod:`common.bi_web.derived`，这是
    「口径后端化」的可测边界。``grain`` 为 ``people``（人员，含部门）或
    ``region``（区域汇总）；``region`` 参数只对人员粒度生效：**缺省
    （None）= 线下线全域**（排除 qudao/vanke/电商，P0-1），显式给值则
    逐字透传该区域（l2-ecom-people 的集合口径入口）。
    """
    if grain not in _SHORTFALL_GRAINS:
        grain = _DEFAULT_SHORTFALL_GRAIN
    if grain == "region":
        rows = _fetch_rows(
            connection, _SHORTFALL_REGION_SQL,
            (first_day, last_day, first_day, last_day),
        )
        if scope is not None:
            # 行级 scope（M3）：区域汇总 SQL 无 WHERE 且按 region 分组，
            # 逐行过滤与下推 region IN 等价（HTTP 侧 grain 未放开，此路径
            # 目前只有单测到达；仍按同一契约消费 scope，不留绕过口）。
            rows = [row for row in rows if row.get("region") in scope]
        return rows
    if region:
        region_sql, region_params = _scope_region_filter(scope, region)
    else:
        # P0-1（2026-09-28）：缺省 = 线下线全域。L1 的缺口/告警卡不带
        # region 参数，原「空片段=全区域」把 qudao 电商集合口径人员
        # （23 人、月目标合计 1.62 亿）整个混进缺口榜；显式选 region
        # （如 l2-ecom-people 选 qudao）仍逐字透传，不受本过滤影响。
        # 行级 scope（M3）叠加在线过滤之后：全域口径再收窄到 scope 内
        # 区域；scope=None 时片段为空，语句与参数逐字节不变。
        fragment, region_params = _scope_region_filter(scope)
        region_sql = _append_region_sql(_OFFLINE_LINE_SQL, fragment)
    sql = _SHORTFALL_PEOPLE_SQL.replace("{region_sql}", region_sql)
    return _fetch_rows(
        connection, sql,
        (first_day, last_day) + region_params
        + (first_day, last_day) + region_params,
    )


def _has_fact(fact) -> bool:
    """诊断标记：``has_fact`` 由 SQL 的 LEFT JOIN 右表主键是否为空给出。

    **不参与 severity 判定**：在这张卡里左表由 target 驱动（有目标才出现在
    结果集），右表为空 == 「有目标、本月截至今日无销单」== 挂零，与
    ``done == 0`` 是同一批人。把它当成「数据缺失」降级，就是把挂零粉饰成
    没数据 —— 漏报比错报严重。真正的第 5 类缺陷需要**独立的人员状态信号**
    （离职/未启用/数据源未接入），目前不存在，不能靠 JOIN 空值倒推。

    字段缺失（旧调用方/老夹具）时按 ``True`` 处理以保持既有行为；SQL 侧
    ``(MAX(d.person) IS NOT NULL)`` 返回 0/1，这里统一转成 ``bool``。
    """
    value = fact.get("has_fact")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return True


def shortfall_rows(facts, *, total_workdays, elapsed_workdays,
                   remaining_workdays) -> list:
    """纯组装：事实行 → CubeSchema §2 行（派生列全部在此调用 derived）。

    排序键只有 ``shortfall``（降序，§2.1「默认排序键」），缺口不可算的行
    （``shortfall`` 为 ``None``：无目标）排末尾 —— 它们是数据缺陷，不是
    落后人员。

    ``has_fact`` 是**纯诊断字段**（``False`` = 本月截至今日无销单行），原值
    透传给前端做角标，**不参与任何派生计算**：无论它真假，``done`` 都按事实
    行的聚合值（缺失即 0）走 §2.3 分级，该 p0 就 p0。
    """
    rows = []
    for fact in facts:
        target = fact.get("target")
        done = fact.get("done")
        required = derived.required_daily(target, total_workdays)
        gap = derived.shortfall(target, done)
        row = {
            key: value for key, value in fact.items()
            if key not in ("target", "done", "has_fact")
        }
        row.update(
            {
                "target": None if target is None else float(target),
                "done": float(done or 0),
                "shortfall": None if gap is None else float(gap),
                "rate": derived.rate(done, target),
                "required_daily": None if required is None else float(required),
                "severity": derived.severity(
                    done, gap, required, remaining_workdays, elapsed_workdays
                ),
                # 纯诊断字段（不参与上面的任何计算）：False = 本月截至今日
                # 无销单行，与 done == 0 同义，前端可据此渲染「本月无销单」角标。
                "has_fact": _has_fact(fact),
                "remaining_workdays": remaining_workdays,
                "elapsed_workdays": elapsed_workdays,
            }
        )
        rows.append(row)
    rows.sort(
        key=lambda row: (
            row["shortfall"] is None,
            -(row["shortfall"] or 0.0),
            row.get("name") or "",
        )
    )
    return rows


def shortfall_calendar(connection, *, first_day, last_day) -> dict:
    """缺口径的日历上下文：``total/elapsed/remaining_workdays``。

    已闭月（今天不在该月内）→ ``remaining_workdays`` 为 ``None``：历史月
    的「剩余产能」没有意义，p1 随之不可判（``derived.severity`` 退到
    p2），而不是把上个月的每个人都标成 p1。
    """
    workdays = month_workdays(connection, first_day=first_day, last_day=last_day)
    as_of = _shortfall_as_of(first_day, last_day)
    today = datetime.now().date()
    return {
        "total_workdays": derived.total_workdays(workdays),
        "elapsed_workdays": derived.elapsed_workdays(workdays, as_of),
        "remaining_workdays": (
            derived.remaining_workdays(workdays, as_of)
            if first_day <= today <= last_day else None
        ),
    }


def shortfall_columns(grain) -> list:
    """卡面列定义（Schema 驱动渲染：前端按 format 格式化，不做换算）。"""
    label = "姓名" if grain == "people" else "区域"
    columns = [{"key": "name", "title": label}]
    if grain == "people":
        columns.append({"key": "dept", "title": "部门"})
    columns.extend(
        [
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "done", "title": "已完成", "format": "wan"},
            {"key": "shortfall", "title": "缺口", "format": "wan"},
            {"key": "rate", "title": "完成率", "format": "percent"},
            {"key": "required_daily", "title": "所需日均", "format": "wan"},
            {"key": "severity", "title": "告警", "format": "severity"},
        ]
    )
    return columns


def _shortfall_window(params):
    """run 层共用解析：month（缺省当前月）+ 月界（``month_bounds``）。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    return month, first_day, last_day


def run_kpi_shortfall(connection, params) -> dict:
    """⑮ 目标缺口与告警表（CubeSchema §2）: target/done/shortfall/rate/
    required_daily/severity，**全部后端派生**。

    region/month 可选；内部已支持 ``grain=region`` 的区域汇总。URL 暂不放
    开 grain 参数：新增一个筛选 source 就要改 ``app._FILTER_SOURCE_QUERIES``
    与 ``config.KNOWN_FILTER_SOURCES``，而 app 层本批冻结，留给下一批增量。
    """
    grain = params.get("grain") or _DEFAULT_SHORTFALL_GRAIN
    if grain not in _SHORTFALL_GRAINS:
        grain = _DEFAULT_SHORTFALL_GRAIN
    month, first_day, last_day = _shortfall_window(params)
    facts = shortfall_facts(
        connection,
        grain=grain,
        region=params.get("region"),
        first_day=first_day,
        last_day=last_day,
        scope=scope_from_params(params),
    )
    calendar = shortfall_calendar(
        connection, first_day=first_day, last_day=last_day
    )
    rows = shortfall_rows(facts, **calendar)
    return {
        "chart": "table",
        "unit": _UNIT,
        "month": month,
        "grain": _SHORTFALL_FRONT_GRAIN[grain],
        "as_of": _shortfall_as_of(first_day, last_day).isoformat(),
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "columns": shortfall_columns(grain),
        "rows": rows,
    }


def run_anomaly_top(connection, params) -> dict:
    """⑯ 缺口 TOP N（AnomalyList）: 与 ⑮ 同口径，服务端按缺口降序截断。

    无目标的行（缺口不可算）**不参与**排序（§5 数据缺陷显式化）；名次由
    服务端给出（``rank`` 1..N），前端不重排。
    """
    month, first_day, last_day = _shortfall_window(params)
    facts = shortfall_facts(
        connection, grain=_DEFAULT_SHORTFALL_GRAIN,
        region=params.get("region"),
        first_day=first_day, last_day=last_day,
        scope=scope_from_params(params),
    )
    calendar = shortfall_calendar(
        connection, first_day=first_day, last_day=last_day
    )
    ranked = [row for row in shortfall_rows(facts, **calendar)
              if row["shortfall"] is not None][:_ANOMALY_TOP_N]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    columns = [{"key": "rank", "title": "名次"}] + shortfall_columns(
        _DEFAULT_SHORTFALL_GRAIN
    )
    return {
        "chart": "table",
        "unit": _UNIT,
        "month": month,
        "grain": _SHORTFALL_FRONT_GRAIN[_DEFAULT_SHORTFALL_GRAIN],
        "as_of": _shortfall_as_of(first_day, last_day).isoformat(),
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "limit": _ANOMALY_TOP_N,
        "columns": columns,
        "rows": ranked,
    }

__all__ = ('_SHORTFALL_GRAINS', '_SHORTFALL_FRONT_GRAIN', '_DEFAULT_SHORTFALL_GRAIN', '_SHORTFALL_PEOPLE_SQL', '_SHORTFALL_REGION_SQL', '_ANOMALY_TOP_N', 'shortfall_facts', '_has_fact', 'shortfall_rows', 'shortfall_calendar', 'shortfall_columns', '_shortfall_window', 'run_kpi_shortfall', 'run_anomaly_top')
