# -*- coding: utf-8 -*-
"""商品结构化查询 + 结果渲染（P1 模板渲染，零 LLM）。

查询顺序：编码/69码精确 → 品名精确 → 品名包含。命中多款由调用方
走反问确认（render_clarify），不猜。
"""

import json

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

    整词 miss 后的回退链（2026-10-22 精度修复）：
    1. 多词 AND 组合——所有分词同时命中品名（「53°500ml*6 习酒窖藏1988
       新包装 2025款」整词带空格必 miss，单最长词「53°500ml*6」回退曾
       命中同规格的千峰翠韵张冠李戴；AND 组合下四个词同时约束直接唯一）；
    2. 单词回退——CJK 词优先于规格词（度数/容量型 token 判别力最低），
       同组内长词优先，最多试 3 个；
    3. 字符 bigram 模糊兜底（2026-10-23 用户反馈「错别字/多几个字就
       匹配不出」）——前三段全是字符串包含，错一个字必 miss；bigram
       集合对单字替换天然鲁棒。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = _search(conn, kw, limit)
    if rows:
        return _decode_json_fields(rows)
    tokens = sorted(
        (t for t in kw.split() if len(t) >= 2), key=_token_rank
    )
    if len(tokens) >= 2:
        rows = _search_all_tokens(conn, tokens[:4], limit)
        if rows:
            return _decode_json_fields(rows)
    for token in tokens[:3]:
        rows = _search(conn, token, limit)
        if rows:
            return _decode_json_fields(rows)
    return _decode_json_fields(_search_fuzzy(conn, kw, limit))


#: 模糊兜底最低分（max(Dice, 关键词覆盖率)）。错别字案例实测 0.8+、
#: 品名片段/带残留案例关键词覆盖率 ≈1.0，0.6 以下基本是无关词，宁缺毋滥。
_FUZZY_MIN_SCORE = 0.6


def _bigrams(text):
    """字符 bigram 集合（模糊匹配用；单字退化为 {字} 防空集）。"""
    text = "".join((text or "").split())
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _search_fuzzy(conn, kw, limit):
    """字符 bigram 相似度兜底：对错别字与口语多字残留容错。

    score = max(Dice, 关键词覆盖率)——Dice 治单字替换（两边长度相近时
    重合度高）；关键词覆盖率（交集中 bigram 数 / 关键词 bigram 数）治
    「关键词是品名片段或带少量残留」场景：关键词的 bigram 基本都能在
    品名里找到 = 用户基本说对了名字。全表 825 行纯 Python 毫秒级。
    """
    kw_grams = _bigrams(kw)
    if not kw_grams:
        return []
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT `name` FROM `kb_products`")
        names = [row["name"] for row in cursor.fetchall()]
        scored = []
        for name in names:
            name_grams = _bigrams(name)
            if not name_grams:
                continue
            overlap = len(kw_grams & name_grams)
            if not overlap:
                continue
            dice = 2 * overlap / (len(kw_grams) + len(name_grams))
            kw_coverage = overlap / len(kw_grams)
            score = max(dice, kw_coverage)
            if score >= _FUZZY_MIN_SCORE:
                scored.append((score, name))
        if not scored:
            return []
        scored.sort(reverse=True)
        top_names = [name for _, name in scored[:limit]]
        placeholders = ",".join(["%s"] * len(top_names))
        cursor.execute(
            f"SELECT * FROM `kb_products` WHERE `name` IN ({placeholders})",
            tuple(top_names),
        )
        rows = {row["name"]: row for row in cursor.fetchall()}
        return [rows[name] for name in top_names if name in rows]
    finally:
        cursor.close()


def _token_rank(token):
    """分词排序键：含 CJK 的词在前（品名判别力高），同类内长词优先。"""
    has_cjk = any("一" <= ch <= "鿿" for ch in token)
    return (0 if has_cjk else 1, -len(token))


def _search_all_tokens(conn, tokens, limit):
    """多词 AND 组合检索：每个词都必须命中品名（交集语义）。"""
    likes = [f"%{_like_escape(t)}%" for t in tokens]
    clause = " AND ".join(["`name` LIKE %s ESCAPE '\\\\'"] * len(tokens))
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT * FROM `kb_products` WHERE {clause}"
            " ORDER BY `name` LIMIT %s",
            (*likes, limit),
        )
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _decode_json_fields(rows):
    """JSON 文本列（attachments/extra）原位解码为 dict（单一真源）。

    sync_products 入库时 json.dumps 存字符串，而 SELECT * 原样返回——
    消费方按 dict 用（``attachments.items()``）会在有附件的商品上炸
    AttributeError（2026-10-21「质检报告」问答必炸事故）。解码失败置
    None（与缺列一致），不阻断查询。
    """
    for row in rows:
        for column in ("attachments", "extra"):
            value = row.get(column)
            if isinstance(value, str):
                try:
                    row[column] = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    row[column] = None
    return rows


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
    """命中多款时反问确认，不猜。

    每行带商品编码（2026-10-23 对话层优化）：同名款的差异藏在长名字的
    括号里（版本/年份），纯名字列表用户还得逐行比对；编码是客服熟知的
    短标识，也可以直接回编码选中（_parse_choice 认编码）。
    """
    lines = [f"「{keyword}」匹配到 {len(rows)} 款，你要问的是哪一个？"]
    for index, row in enumerate(rows, 1):
        code = row.get("code")
        suffix = f"（编码：{code}）" if code else ""
        lines.append(f"{index}. {row['name']}{suffix}")
    lines.append("回序号或编码都可以。")
    return "\n".join(lines)


def render_not_found(keyword):
    return (
        f"知识库里暂时没有找到「{keyword}」。你可以：\n"
        "1. 换个说法再问一次\n"
        "2. 把商品编码或完整名称发我，我精确查一下"
    )


HELP_TEXT = (
    "我是小易，可以直接问我：\n"
    "1. 商品参数与筛选：如「习酒窖藏1998 箱规」「有哪些酱香酒」\n"
    "2. 商品素材：如「发我摘要酒细节图」「有质检报告吗」\n"
    "3. 产品手册：如「摘要酒怎么介绍」「习酒有哪些卖点」\n"
    "4. 号码台账：如「尾号5579是谁在用」「这个店铺账号验证码找谁」\n"
    "5. 订单物流：如「订单JY202501010120发货没」「这个快递到哪了」\n"
    "6. 业务数据：如「杭州昨天销售额」「各区域完成率排名」"
)
