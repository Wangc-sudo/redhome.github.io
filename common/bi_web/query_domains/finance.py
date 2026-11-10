"""BI finance 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_FIN_RECEIVABLES_OVERDUE_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(overdue_amount), 0) AS overdue,
           COALESCE(SUM(ending_balance), 0) AS balance
    FROM fact_fin_receivables_aging
    """
).strip()


_FIN_RECEIVABLES_AGING_SQL = textwrap.dedent(
    """
    SELECT counterparty_name, receivable_category, company_entity,
           ending_balance, overdue_amount, aging_0_30, aging_31_60,
           updated_date
    FROM fact_fin_receivables_aging
    ORDER BY overdue_amount DESC, counterparty_name
    """
).strip()


_FIN_PREPAYMENT_UNINVOICED_SQL = textwrap.dedent(
    """
    SELECT supplier_name, company_entity, prepayment_ledger_amount,
           ap_estimated_amount, ledger_reconciliation_status,
           uninvoiced_amount, statement_date
    FROM fact_fin_prepayment_invoice
    ORDER BY uninvoiced_amount DESC, supplier_name
    """
).strip()


_FIN_DEPOSIT_STATUS_SQL = textwrap.dedent(
    """
    SELECT company_entity, supplier_name AS counterparty, project_name,
           cooperation_status AS status, deposit_balance, updated_at,
           '线下' AS source
    FROM fact_fin_offline_deposit
    UNION ALL
    SELECT company_entity, store_name AS counterparty, project_name,
           store_operating_status AS status, deposit_balance, NULL AS updated_at,
           '平台' AS source
    FROM fact_fin_platform_deposit
    ORDER BY deposit_balance DESC
    """
).strip()


_FIN_STORE_FUNDS_SQL = textwrap.dedent(
    """
    SELECT month, store_name, channel, balance
    FROM fact_fin_store_funds
    ORDER BY month, store_name
    """
).strip()


def _iso_or_none(value):
    """date/datetime → ISO 字符串；None 原样透传（不猜日期）。"""
    if value is None:
        return None
    return value.isoformat()


def _float_or_none(value):
    """Decimal → float；None 原样透传（§5：不静默补 0）。"""
    return None if value is None else float(value)


def run_kpi_fin_receivables_overdue(connection, params) -> dict:
    """⑩ 应收逾期总览: Σ逾期金额(value) / Σ期末余额(target) / 逾期占比(rate)。

    rate = overdue ÷ balance，后端除法 + 除零护栏（balance 为 0 → None，
    前端「—」）；聚合查询恒有一行，COALESCE 兜底空表为 0。
    """
    rows = _fetch_rows(connection, _FIN_RECEIVABLES_OVERDUE_SQL)
    row = rows[0] if rows else {}
    overdue = Decimal(row.get("overdue") or 0)
    balance = Decimal(row.get("balance") or 0)
    rate = None if balance == 0 else float(overdue) / float(balance)
    return {
        "chart": "scalar",
        "value": float(overdue),
        "target": float(balance),
        "rate": rate,
        "unit": _UNIT,
    }


def run_table_fin_receivables_aging(connection, params) -> dict:
    """⑩ 应收账龄明细: 含差额推导的 >60 天桶与超期告警。

    ``aging_over_60`` 与 ``severity`` 由 :mod:`fin_derived` 派生（SQL 不碰）；
    ``updated_date`` 原样透传（账龄表编制日期，页面角标用）。
    """
    rows = _fetch_rows(connection, _FIN_RECEIVABLES_AGING_SQL)
    table_rows = []
    for row in rows:
        over_60 = fin_derived.aging_over_60(
            row.get("ending_balance"), row.get("aging_0_30"),
            row.get("aging_31_60"),
        )
        table_rows.append(
            {
                "counterparty": row.get("counterparty_name"),
                "category": row.get("receivable_category"),
                "entity": row.get("company_entity"),
                "ending_balance": _float_or_none(row.get("ending_balance")),
                "overdue_amount": _float_or_none(row.get("overdue_amount")),
                "aging_0_30": _float_or_none(row.get("aging_0_30")),
                "aging_31_60": _float_or_none(row.get("aging_31_60")),
                "aging_over_60": over_60,
                "updated_date": _iso_or_none(row.get("updated_date")),
                "severity": fin_derived.aging_severity(
                    over_60, row.get("overdue_amount")
                ),
            }
        )
    return {
        "chart": "table",
        "unit": _UNIT,
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "columns": [
            {"key": "counterparty", "title": "往来单位"},
            {"key": "category", "title": "应收类别"},
            {"key": "entity", "title": "公司主体"},
            {"key": "ending_balance", "title": "期末余额", "format": "wan"},
            {"key": "overdue_amount", "title": "逾期金额", "format": "wan"},
            {"key": "aging_0_30", "title": "账龄0-30天", "format": "wan"},
            {"key": "aging_31_60", "title": "账龄31-60天", "format": "wan"},
            {"key": "aging_over_60", "title": "账龄>60天", "format": "wan"},
            {"key": "updated_date", "title": "编制日期"},
            {"key": "severity", "title": "告警", "format": "severity"},
        ],
        "rows": table_rows,
    }


def run_table_fin_prepayment_uninvoiced(connection, params) -> dict:
    """⑩ 预付与未到票: 挂账天数（>60 天告警）与未到票金额。

    ``days_outstanding`` = 今天 − 对账日（自然日），对账日不可解析
    → None（不猜日期）；``ledger_reconciliation_status`` 原样透传
    （账账核对枚举清单待财务给，登记不阻塞）。
    """
    today = datetime.now().date()
    rows = _fetch_rows(connection, _FIN_PREPAYMENT_UNINVOICED_SQL)
    table_rows = []
    for row in rows:
        days = fin_derived.days_outstanding(row.get("statement_date"), today)
        table_rows.append(
            {
                "supplier": row.get("supplier_name"),
                "entity": row.get("company_entity"),
                "prepayment": _float_or_none(row.get("prepayment_ledger_amount")),
                "ap_estimated": _float_or_none(row.get("ap_estimated_amount")),
                "reconciliation": row.get("ledger_reconciliation_status"),
                "uninvoiced": _float_or_none(row.get("uninvoiced_amount")),
                "statement_date": _iso_or_none(row.get("statement_date")),
                "days_outstanding": days,
                "severity": fin_derived.uninvoiced_severity(
                    row.get("uninvoiced_amount"), days
                ),
            }
        )
    return {
        "chart": "table",
        "unit": _UNIT,
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "columns": [
            {"key": "supplier", "title": "供应商"},
            {"key": "entity", "title": "公司主体"},
            {"key": "prepayment", "title": "预付账款", "format": "wan"},
            {"key": "ap_estimated", "title": "应付暂估", "format": "wan"},
            {"key": "reconciliation", "title": "账账核对"},
            {"key": "uninvoiced", "title": "未到票金额", "format": "wan"},
            {"key": "statement_date", "title": "对账日期"},
            {"key": "days_outstanding", "title": "挂账天数", "format": "number"},
            {"key": "severity", "title": "告警", "format": "severity"},
        ],
        "rows": table_rows,
    }


def run_table_fin_deposit_status(connection, params) -> dict:
    """⑩ 保证金状态: 线下 + 平台合一，状态原文透传（枚举不固化）。

    severity：非正常合作/运营状态 → p2 关注（可退未退），正常 → ok；
    判定规则保守化在 :func:`fin_derived.deposit_severity`（「正常」取值
    清单待财务/运营确认）。
    """
    rows = _fetch_rows(connection, _FIN_DEPOSIT_STATUS_SQL)
    table_rows = [
        {
            "entity": row.get("company_entity"),
            "counterparty": row.get("counterparty"),
            "project": row.get("project_name"),
            "status": row.get("status"),
            "deposit": _float_or_none(row.get("deposit_balance")),
            "updated_at": _iso_or_none(row.get("updated_at")),
            "source": row.get("source"),
            "severity": fin_derived.deposit_severity(row.get("status")),
        }
        for row in rows
    ]
    return {
        "chart": "table",
        "unit": _UNIT,
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "columns": [
            {"key": "entity", "title": "公司主体"},
            {"key": "counterparty", "title": "往来对象"},
            {"key": "project", "title": "项目"},
            {"key": "status", "title": "状态"},
            {"key": "deposit", "title": "保证金余额", "format": "wan"},
            {"key": "updated_at", "title": "更新时间"},
            {"key": "source", "title": "来源"},
            {"key": "severity", "title": "告警", "format": "severity"},
        ],
        "rows": table_rows,
    }


def run_trend_fin_store_funds(connection, params) -> dict:
    """⑩ 店铺资金余额趋势: 月升序轴 × 店铺系列（零填充）。

    与 ``_align_series_rows`` 同款零填充思路：轴为窄表去重后的全部月份
    （``YYYY-MM`` 字符串，天然可排序），店铺在某月无行 → 0。
    """
    rows = _fetch_rows(connection, _FIN_STORE_FUNDS_SQL)
    months = sorted({row["month"] for row in rows if row.get("month")})
    index_of = {month: index for index, month in enumerate(months)}
    by_store = {}
    for row in rows:
        month = row.get("month")
        if month is None:
            continue
        name = row.get("store_name") or ""
        by_store.setdefault(name, [Decimal(0)] * len(months))[
            index_of[month]
        ] = Decimal(row.get("balance") or 0)
    return {
        "chart": "line",
        "dates": months,
        "series": [
            {"name": name, "data": [float(value) for value in by_store[name]]}
            for name in sorted(by_store)
        ],
        "unit": _UNIT,
    }


_FIN_STORE_FUNDS_ENTITY_SQL = textwrap.dedent(
    """
    SELECT month, channel, SUM(balance) AS balance
    FROM fact_fin_store_funds
    WHERE company_entity = %s
    GROUP BY month, channel
    ORDER BY month
    """
).strip()


_FIN_STORE_FUNDS_CHANNEL_SQL = textwrap.dedent(
    """
    SELECT month, channel, SUM(balance) AS balance
    FROM fact_fin_store_funds
    GROUP BY month, channel
    ORDER BY month
    """
).strip()


_FIN_STORE_ENTITY_OPTIONS_SQL = textwrap.dedent(
    """
    SELECT DISTINCT company_entity
    FROM fact_fin_store_funds
    ORDER BY company_entity
    """
).strip()


def entity_options(connection) -> list:
    """筛选器「公司主体」下拉：DISTINCT company_entity，升序。"""
    return [
        row["company_entity"]
        for row in _fetch_rows(connection, _FIN_STORE_ENTITY_OPTIONS_SQL)
        if row["company_entity"]
    ]


def clean_channel_label(value) -> str:
    """钉钉 singleSelect 落库是 Python dict 字面量（``{'name': '拼多多', 'id': '…'}``）。

    取 ``name`` 作为可读渠道名；解析失败或本就是纯文本则原样返回。空值
    返回空串，由调用方归一成「未分类」——不猜、不丢弃。
    """
    raw = (value or "").strip()
    if not raw or not raw.startswith("{"):
        return raw
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw
    if isinstance(parsed, dict):
        name = parsed.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return raw


def _funds_channel_trend_payload(rows) -> dict:
    """(month, channel, Σbalance) 聚合行 → 渠道多系列趋势载荷（零填充）。

    与 ``_align_series_rows`` 同款思路：轴为去重后的全部月份
    （``YYYY-MM`` 字符串，天然可排序），渠道在某月无行 → 0；渠道名先经
    :func:`clean_channel_label` 清洗（钉钉 singleSelect dict 字面量）。
    """
    months = sorted({row["month"] for row in rows if row.get("month")})
    index_of = {month: index for index, month in enumerate(months)}
    by_channel = {}
    for row in rows:
        month = row.get("month")
        if month is None:
            continue
        key = clean_channel_label(row.get("channel")) or "未分类"
        bucket = by_channel.setdefault(key, [Decimal(0)] * len(months))
        bucket[index_of[month]] += Decimal(row.get("balance") or 0)
    return {
        "chart": "line",
        "dates": months,
        "series": [
            {"name": name,
             "data": [None if value is None else float(value)
                      for value in by_channel[name]]}
            for name in sorted(by_channel)
        ],
        "unit": _UNIT,
    }


def run_trend_fin_store_funds_entity(connection, params) -> dict:
    """⑩ 店铺资金余额趋势（主体参数化）: entity 单主体 / 空=全主体，按渠道汇总。

    38 家店铺画在一张图上 = 38 条 series，完全不可读；公司主体会变
    （新增/改名/下线），故 4 张硬编码分屏卡收敛为本参数化卡 +
    ``entities`` 筛选源（2026-09-17 P2）。聚合（WHERE + GROUP BY）已
    下推 SQL，Python 只留渠道名清洗与缺月零填充；entity 经 app 层
    ``entities`` 值域闸校验，非法值 400。
    """
    entity = (params.get("entity") or "").strip()
    if entity:
        rows = _fetch_rows(connection, _FIN_STORE_FUNDS_ENTITY_SQL, (entity,))
    else:
        rows = _fetch_rows(connection, _FIN_STORE_FUNDS_CHANNEL_SQL)
    return _funds_channel_trend_payload(rows)

__all__ = ('_FIN_RECEIVABLES_OVERDUE_SQL', '_FIN_RECEIVABLES_AGING_SQL', '_FIN_PREPAYMENT_UNINVOICED_SQL', '_FIN_DEPOSIT_STATUS_SQL', '_FIN_STORE_FUNDS_SQL', '_iso_or_none', '_float_or_none', 'run_kpi_fin_receivables_overdue', 'run_table_fin_receivables_aging', 'run_table_fin_prepayment_uninvoiced', 'run_table_fin_deposit_status', 'run_trend_fin_store_funds', '_FIN_STORE_FUNDS_ENTITY_SQL', '_FIN_STORE_FUNDS_CHANNEL_SQL', '_FIN_STORE_ENTITY_OPTIONS_SQL', 'entity_options', 'clean_channel_label', '_funds_channel_trend_payload', 'run_trend_fin_store_funds_entity')
