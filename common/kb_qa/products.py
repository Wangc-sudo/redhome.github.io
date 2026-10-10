# -*- coding: utf-8 -*-
"""商品结构化查询 + 结果渲染（P1 模板渲染，零 LLM）。

查询顺序：编码/69码精确 → 品名精确 → 品名包含。命中多款由调用方
走反问确认（render_clarify），不猜。
"""

#: 展示列（中文名, 列名），按客服关心度排序。
PARAM_COLUMNS = (
    ("商品编码", "code"),
    ("品牌", "brand"),
    ("大类", "category"),
    ("箱规", "box_qty"),
    ("盒规", "case_qty"),
    ("单盒尺寸(mm)", "size_mm"),
    ("重量(kg)", "weight_kg"),
    ("材质", "material"),
    ("礼袋规格", "gift_bag_spec"),
    ("单品69码", "barcode_single"),
    ("原箱69码", "barcode_case"),
)

_SOURCE_TAG = "【商品资料】"
_MAX_ROWS = 8


def _like_escape(text):
    """LIKE 字面量转义（ESCAPE '\\'）：\\ % _ 三字符。"""
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def find_products(conn, keyword, *, limit=_MAX_ROWS):
    """按关键词找商品，返回 DictCursor 行列表（可能为空）。

    整词 miss 后按分词回退（长词优先，最多试 3 个）：用户消息常带
    无关后缀（「习酒窖藏1998 箱规」验收），整词 LIKE 必 miss，
    分词「习酒窖藏1998」能中（2026-10-10 验收事故）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = _search(conn, kw, limit)
    if rows:
        return rows
    tokens = sorted(
        (t for t in kw.split() if len(t) >= 2), key=len, reverse=True
    )
    for token in tokens[:3]:
        rows = _search(conn, token, limit)
        if rows:
            return rows
    return []


def _search(conn, kw, limit):
    """三级检索：编码/69码精确 → 品名精确 → 品名/品牌/大类包含。

    第三段搜三个字段（2026-10-10「有哪些酱香酒」盲区修复）：category
    实测是产品系列（窖藏1988/知交系列）而非香型，「酱香酒」只在 brand
    「茅台酱香酒」上能中——只搜品名会系统性漏掉品牌/系列维度。
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM `kb_products`"
            " WHERE `code`=%s OR `barcode_single`=%s OR `barcode_case`=%s"
            " LIMIT %s",
            (kw, kw, kw, limit),
        )
        rows = list(cursor.fetchall())
        if rows:
            return rows
        cursor.execute(
            "SELECT * FROM `kb_products` WHERE `name`=%s LIMIT %s",
            (kw, limit),
        )
        rows = list(cursor.fetchall())
        if rows:
            return rows
        like = f"%{_like_escape(kw)}%"
        cursor.execute(
            "SELECT * FROM `kb_products`"
            " WHERE `name` LIKE %s ESCAPE '\\\\'"
            " OR `brand` LIKE %s ESCAPE '\\\\'"
            " OR `category` LIKE %s ESCAPE '\\\\'"
            " ORDER BY `name` LIMIT %s",
            (like, like, like, limit),
        )
        return list(cursor.fetchall())
    finally:
        cursor.close()


def render_param_answer(row):
    """单个商品的参数卡：全量非空字段，客服可直接复制转发。"""
    lines = [f"📦 {row['name']}"]
    for label, column in PARAM_COLUMNS:
        value = row.get(column)
        if value is None or value == "":
            continue
        lines.append(f"{label}：{value}")
    lines.append(_SOURCE_TAG)
    return "\n".join(lines)


def render_filter_answer(rows, keyword):
    """筛选结果列表：一名一行，附品牌/大类便于区分；满上限给截断提示。"""
    lines = [f"「{keyword}」匹配到 {len(rows)} 款："]
    for index, row in enumerate(rows, 1):
        parts = [row["name"]]
        if row.get("brand"):
            parts.append(row["brand"])
        if row.get("category"):
            parts.append(row["category"])
        lines.append(f"{index}. {' / '.join(str(p) for p in parts)}")
    if len(rows) >= _MAX_ROWS:
        lines.append(f"（仅列前 {_MAX_ROWS} 款，发品牌名或品名可精确查）")
    lines.append("发具体品名可查参数 " + _SOURCE_TAG)
    return "\n".join(lines)


def render_clarify(keyword, rows):
    """命中多款时反问确认，不猜。"""
    lines = [f"「{keyword}」匹配到 {len(rows)} 款，你要问的是哪一个？"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {row['name']}")
    return "\n".join(lines)


def render_not_found(keyword):
    return (
        f"知识库里暂时没有找到「{keyword}」。你可以：\n"
        "1. 换个说法再问一次\n"
        "2. 把商品编码或完整名称发我，我精确查一下"
    )


HELP_TEXT = (
    "我是产品资料助手，可以直接问我：\n"
    "1. 商品参数：如「习酒窖藏1998 箱规」「摘要酒 多重」\n"
    "2. 商品筛选：如「有哪些酱香酒」\n"
    "3. 商品素材：如「发我摘要酒细节图」「有质检报告吗」\n"
    "产品手册问答（卖点/话术）接入中，敬请期待。"
)
