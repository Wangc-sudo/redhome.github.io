"""rag-index-wdt：WDT 交易/出库/退款单据 → 检索文本块清洗管道（样本产出版）。

定位：小易知识库新阶段的数据清洗侧。把 raw_wdt.wdt_records 里的结构化
单据 payload 拍平成自然语言文本块，供后续 embedding 入 kb_rag_v1。

本版本只产出样本（--limit 控制条数，打印到 stdout + 可选 --out 落 JSONL），
**不连接向量库、不调用 embedding**——用于先把「洗成什么样」对齐，再决定
灌库范围与 B 模型选型（ctaigw / jina）。

清洗口径（2026-10-27 实测 payload 校准）：
- 交易级时间 trade_time/created 为 epoch 毫秒；pay_time/check_time 为
  'YYYY-MM-DD HH:MM:SS' 字符串，统一转可读日期。
- 买家标识取交易级 customer_id（2026-10-12 实测，非 buyer_id）；B2B 客户
  主体在 buyer_nick（天猫寄售等），B2C 零售在 customer_name。
- 明细取 detail_list[]：goods_name / spec_name / num / paid / spec_no。
- 单引号风格 payload 双段兜底：json.loads → ast.literal_eval
  （与 extract_mart.py 同一坑位）。
- 幂等 id 设计：``wdt-trade:{trade_no}``，后续灌库 upsert = 先 delete 后
  insert，重跑无脏数据（对齐 index_products 数据层幂等）。
"""

import argparse
import ast
import datetime
import json

from common.public_data import db, extract_mart
from common.public_data.settings import Settings


#: 交易级文本拼接字段（payload 键, 标签）。空值跳过。
_TRADE_FIELDS = (
    ("shop_name", "店铺"),
    ("trade_status", "状态"),
    ("buyer_nick", "客户"),
    ("customer_name", "买家姓名"),
    ("customer_no", "客户编号"),
    ("salesman_name", "业务员"),
    ("receiver_area", "收货地区"),
    ("logistics_name", "物流"),
    ("logistics_no", "物流单号"),
    ("cs_remark", "客服备注"),
    ("refund_status", "退款状态"),
)

_TRADE_METHOD = "sales.TradeQuery.queryWithDetail"

#: 交易状态码可读化（2026-10-27 线上全量统计：110 占 81%、5 占 15%、95/30/55
#: 为长尾，4/96/27/24/16/20/6 极少量）。键为字符串，与 JSON 读出口径一致。
_TRADE_STATUS_LABEL = {
    "110": "已发货",
    "5": "已取消",
    "95": "已完成",
    "30": "已付款待审核",
    "55": "部分发货",
    "4": "待付款",
    "96": "已完成退款",
    "27": "已付款",
    "24": "待审核",
    "16": "已审核",
    "20": "已打印",
    "6": "待处理",
}

#: 退款状态码可读化（2026-10-27 线上全量统计：0=无退款 占 85%、3 占 15%）。
_REFUND_STATUS_LABEL = {
    "0": "无退款",
    "1": "申请退款",
    "2": "部分退款",
    "3": "全部退款",
}


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _parse_payload(raw):
    """单/双引号双段兜底解析 payload_json。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None


def _fmt_epoch_ms(value):
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM'；非纯数字则原样返回（pay_time 等）。"""
    s = _clean(value)
    if not s:
        return None
    if not s.isdigit():
        return s
    try:
        dt = datetime.datetime.fromtimestamp(int(s) / 1000)
    except (ValueError, OSError, OverflowError):
        return s
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_money(value):
    s = _clean(value)
    if not s:
        return None
    try:
        return f"￥{float(s):,.2f}"
    except ValueError:
        return s


def build_line_texts(payload):
    """交易单 payload → 检索文本块列表（**一明细行 = 一块**，2026-10-27 用户裁决
    检索更细）。无 trade_no 或无有效明细返回空列表。

    每块 = 交易级上下文（单号/时间/店铺/客户/状态）+ 单条明细行
    （商品/规格/数量/实付）。幂等 id 为 ``wdt-trade:{trade_no}:{line_no}``。
    """
    if not isinstance(payload, dict):
        return []
    trade_no = _clean(payload.get("trade_no"))
    if not trade_no:
        return []

    # 交易级公共前缀
    ctx = [f"交易单号:{trade_no}"]
    trade_time = _fmt_epoch_ms(payload.get("trade_time")) or _clean(payload.get("pay_time"))
    if trade_time:
        ctx.append(f"时间:{trade_time}")
    for key, label in _TRADE_FIELDS:
        value = _clean(payload.get(key))
        if not value:
            continue
        if key == "trade_status":
            value = f"{_TRADE_STATUS_LABEL.get(value, value)}({value})"
        elif key == "refund_status":
            value = f"{_REFUND_STATUS_LABEL.get(value, value)}({value})"
        ctx.append(f"{label}:{value}")
    prefix = "；".join(ctx)

    blocks = []
    line_no = 0
    for d in payload.get("detail_list") or []:
        if not isinstance(d, dict):
            continue
        name = _clean(d.get("goods_name")) or _clean(d.get("api_goods_name"))
        if not name:
            continue
        line_no += 1
        seg = f"商品:{name}"
        spec = _clean(d.get("spec_name"))
        if spec and spec != name:
            seg += f"；规格:{spec}"
        num = _clean(d.get("num"))
        if num:
            seg += f"；数量:{num}"
        paid = _fmt_money(d.get("paid"))
        if paid:
            seg += f"；实付:{paid}"
        blocks.append((line_no, f"{prefix}；{seg}"))
    return blocks


def iter_line_samples(repository, *, limit, month=None):
    """从 raw_wdt 读交易单，逐条明细行 yield (line_id, text)。

    样本/试灌阶段直接在 wdt_connection 上查询（read_wdt_trades 全表拉取
    75 万行过重）。``month='YYYY-MM'`` 限定 trade_time 落在该月（毫秒戳），
    用于试灌单月。limit 为产出**明细行块**的上限（None 表示不限）。
    """
    params = [_TRADE_METHOD]
    sql = "SELECT `payload_json` FROM `wdt_records` WHERE `source_method` = %s"
    if month:
        year, mon = int(month[:4]), int(month[5:7])
        start_dt = datetime.datetime(year, mon, 1)
        if mon == 12:
            end_dt = datetime.datetime(year + 1, 1, 1)
        else:
            end_dt = datetime.datetime(year, mon + 1, 1)
        sql += (
            " AND CAST(JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.trade_time'))"
            " AS UNSIGNED) >= %s"
            " AND CAST(JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.trade_time'))"
            " AS UNSIGNED) < %s"
        )
        params += [
            int(start_dt.timestamp() * 1000),
            int(end_dt.timestamp() * 1000),
        ]
    conn = repository.wdt_connection
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    produced = 0
    for row in rows:
        payload = _parse_payload(row.get("payload_json"))
        for line_no, text in build_line_texts(payload):
            trade_no = _clean(payload.get("trade_no"))
            yield f"{trade_no}:{line_no}", text
            produced += 1
            if limit is not None and produced >= limit:
                return


def _build_repository(settings):
    """构造 MartExtractRepository：raw/mart/wdt 三连接（样本版只用到 wdt）。"""
    raw_conn = db.connect(settings.dingtalk_database)
    mart_conn = db.connect(settings.mart_database)
    wdt_conn = db.connect(settings.wdt_database)
    return extract_mart.MartExtractRepository(raw_conn, mart_conn, wdt_conn)


def main(argv=None):
    parser = argparse.ArgumentParser(description="WDT 交易单清洗 → 文本块（样本 / 灌库）")
    parser.add_argument("--limit", type=int, default=None,
                        help="产出明细行块上限（默认 None=不限）")
    parser.add_argument("--month", metavar="YYYY-MM",
                        help="限定 trade_time 落月份（试灌单月用）")
    parser.add_argument("--out", metavar="PATH", help="可选：JSONL 样本落盘路径")
    parser.add_argument("--apply", action="store_true",
                        help="灌库模式：走 ctaigw embedding + Milvus upsert")
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_environment()
    repository = _build_repository(settings)

    if not args.apply:
        # 样本模式：打印 + 可选落盘，不碰向量库
        samples = []
        for line_id, text in iter_line_samples(repository, limit=args.limit,
                                               month=args.month):
            item = {"id": f"wdt-trade:{line_id}", "kind": "wdt", "text": text}
            samples.append(item)
            print(f"[{line_id}]\n{text}\n")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                for item in samples:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"已落 {len(samples)} 条样本到 {args.out}")
        print(f"共产出 {len(samples)} 条样本")
        return 0

    return run_apply(repository, month=args.month, limit=args.limit,
                     live_read=args.live_read,
                     confirm_local_test_write=args.confirm_local_test_write)


def run_apply(repository, *, month, limit, live_read, confirm_local_test_write):
    """灌库模式：清洗 → ctaigw embedding → Milvus upsert。

    与 index_products 同一套幂等策略（upsert=delete+insert）与 fail-soft
    （无 embedding key 静默跳过）。批次上限按 provider 取（ctaigw 实测 22 条
    即 HTTP 400——index_products.py 注释的 2026-10-20 教训）。
    """
    import os

    from common.public_data.settings import Settings
    from common.rag import embed, store

    provider = os.environ.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = os.environ.get("RAG_EMBED_API_KEY") or os.environ.get("JINA_API_KEY")
    if not api_key:
        print("service=rag-index-wdt status=skipped reason=no_embed_key")
        return 0
    base_url = os.environ.get("RAG_EMBED_BASE_URL")

    settings = Settings.from_environment()
    from common.public_data.live_safety import require_live_run
    require_live_run(
        settings,
        live_read=live_read,
        confirm_local_test_write=confirm_local_test_write,
    )

    milvus = store.connect(os.environ.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    dim = int(os.environ.get("RAG_EMBED_DIM")
              or os.environ.get("JINA_CLIP_DIM") or embed.DEFAULT_DIM)
    model = (os.environ.get("RAG_EMBED_MODEL")
             or os.environ.get("JINA_CLIP_MODEL")
             or embed.default_model(provider))
    if not model:
        raise SystemExit(f"provider={provider} 需要 RAG_EMBED_MODEL")

    # 全量收集文本块（单月约数万行，可接受内存）
    blocks = [{"id": f"wdt-trade:{line_id}", "text": text}
              for line_id, text in iter_line_samples(repository, limit=limit,
                                                     month=month)]
    real_dim = store.ensure_collection(milvus, dim)
    text_batch = embed.max_batch(provider)
    print(f"service=rag-index-wdt month={month} blocks={len(blocks)} "
          f"provider={provider} model={model} dim={real_dim} batch={text_batch}",
          flush=True)

    items = []
    embedded = 0
    for start in range(0, len(blocks), text_batch):
        chunk = blocks[start:start + text_batch]
        vectors = embed.embed_items(
            [{"text": b["text"]} for b in chunk], api_key,
            model=model, dimensions=real_dim, provider=provider, base_url=base_url)
        for b, vector in zip(chunk, vectors):
            items.append({
                "id": b["id"],
                "vector": vector,
                "kind": "wdt",
                "name": b["text"][:80],
                "text": b["text"],
                "image_ref": "",
            })
        embedded += len(chunk)
        print(f"progress embedded={embedded}/{len(blocks)}", flush=True)

    written = store.upsert_items(milvus, items)
    print(f"service=rag-index-wdt status=completed blocks={len(blocks)} "
          f"written={written}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
