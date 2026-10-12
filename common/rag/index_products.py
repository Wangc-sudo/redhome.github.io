# -*- coding: utf-8 -*-
"""rag-index-products：AI 表「产品资料」→ Milvus 向量索引管道（第一期）。

平台标准形态：
- 调度：scheduler 命令表 ``rag-index-products``；
- 幂等：条目 id = ``product:{record_id}`` / ``product-img:{record_id}:{字段}:{序号}``，
  upsert = 先 delete 后 insert，重跑/手动触发无脏数据（数据层幂等，无需 --force）；
- 门禁：require_live_run（外部 live 读 + 向量库写双确认）；
- 数据源：直接重拉 AI 表记录（kb_products.attachments 只存元信息不存签名
  URL——URL 带时效必须用时现取），复用 sync_products.normalize_record 清洗，
  文本/图片同批进 jina-clip-v2（同一向量空间，支撑文字搜图）。

环境变量（凭据进 /opt/dops/app.env 不入库）：
    JINA_API_KEY / JINA_CLIP_MODEL / JINA_CLIP_DIM / RAG_MILVUS_PATH
    KB_DINGTALK_APP_KEY / KB_DINGTALK_APP_SECRET / KB_DINGTALK_OPERATOR_ID
    KB_PRODUCTS_BASE_ID / KB_PRODUCTS_SHEET_ID
"""

import argparse
import os

from common.kb_qa import media
from common.kb_qa.sync_products import ATTACHMENT_FIELDS, normalize_record
from common.rag import embed

#: 文本条目拼接的列（列名, 标签）：全量核心字段，语义检索素材。
_TEXT_FIELDS = (
    ("code", "编码"), ("name", "名称"), ("brand", "品牌"),
    ("category", "大类"), ("box_qty", "箱规"), ("case_qty", "盒规"),
    ("size_mm", "尺寸"), ("weight_kg", "重量kg"), ("material", "材质"),
    ("gift_bag_spec", "礼袋规格"), ("barcode_single", "单品69码"),
    ("barcode_case", "原箱69码"),
)


def build_text(row):
    """kb_products 行 → 检索文本（核心字段拼接）。normalize_record 保证 name 非空。"""
    parts = []
    for column, label in _TEXT_FIELDS:
        value = row.get(column)
        if value is not None:
            parts.append(f"{label}:{value}")
    return "；".join(parts)


def iter_image_attachments(record):
    """AI 表原始记录 → [(字段名, 序号, {name,url,mime}),...]，只留带 URL 的图片。

    URL 只存在于原始记录（入库 attachments 已丢弃）；非图片（PDF/zip）跳过。
    """
    fields = record.get("fields") or {}
    found = []
    for field_name in ATTACHMENT_FIELDS:
        value = fields.get(field_name)
        if not isinstance(value, (list, tuple)):
            continue
        for index, entry in enumerate(value):
            if not isinstance(entry, dict):
                continue
            name = entry.get("filename") or entry.get("name") or ""
            if not media.is_image_name(name):
                continue
            url = media._extract_attachment_url(entry)
            if not url:
                continue
            found.append((field_name, index, {
                "name": name,
                "url": url,
                "mime": entry.get("type") or entry.get("mimeType"),
            }))
    return found


def run_index(*, client, milvus, store, jina_key, base_id, sheet_id, dim,
              model=embed.DEFAULT_MODEL, text_batch=64, on_progress=None):
    """全量重建索引。返回统计 dict。单张图片失败 fail-soft 跳过记数。"""
    records = client.list_records(base_id, sheet_id)
    pairs = []
    for record in records:
        row = normalize_record(record)
        if row:
            pairs.append((row, record))

    real_dim = store.ensure_collection(milvus, dim)
    items = []
    stats = {"records_read": len(records), "products": len(pairs),
             "text": 0, "image": 0, "image_failed": 0}

    # 文本：批量 embed（jina 单请求多条，按 text_batch 分批）。
    for start in range(0, len(pairs), text_batch):
        chunk = pairs[start:start + text_batch]
        texts = [build_text(row) for row, _ in chunk]
        vectors = embed.embed_items([{"text": t} for t in texts], jina_key,
                                    model=model, dimensions=real_dim)
        for (row, _), vector, text in zip(chunk, vectors, texts):
            items.append({
                "id": f"product:{row['record_id']}",
                "vector": vector,
                "kind": "text",
                "name": row["name"],
                "text": text,
                "image_ref": "",
            })
        stats["text"] += len(chunk)
        if on_progress:
            on_progress("text", stats["text"])

    # 图片：按商品分组下载+批量 embed（该商品的全部图一次 API 调用）。
    for row, record in pairs:
        images = iter_image_attachments(record)
        if not images:
            continue
        try:
            contents = [media.download_bytes(meta["url"]) for _, _, meta in images]
            vectors = embed.embed_items(
                [{"image": embed.to_data_uri(content, meta["mime"])}
                 for content, (_, _, meta) in zip(contents, images)],
                jina_key, model=model, dimensions=real_dim)
        except embed.EmbedError:
            stats["image_failed"] += len(images)
            continue
        for (field_name, index, meta), vector in zip(images, vectors):
            items.append({
                "id": f"product-img:{row['record_id']}:{field_name}:{index}",
                "vector": vector,
                "kind": "image",
                "name": row["name"],
                "text": "",
                "image_ref": f"{row['record_id']}/{field_name}/{index}",
            })
            stats["image"] += 1
        if on_progress:
            on_progress("image", stats["image"])

    stats["written"] = store.upsert_items(milvus, items)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rag.index_products")
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    from common.dingtalk.client import DingTalkClient
    from common.public_data.live_safety import require_live_run
    from common.public_data.settings import Settings
    from common.rag import store

    settings = Settings.from_environment()
    require_live_run(
        settings,
        live_read=args.live_read,
        confirm_local_test_write=args.confirm_local_test_write,
    )
    client = DingTalkClient(
        os.environ["KB_DINGTALK_APP_KEY"],
        os.environ["KB_DINGTALK_APP_SECRET"],
        os.environ["KB_DINGTALK_OPERATOR_ID"],
    )
    milvus = store.connect(os.environ.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    dim = int(os.environ.get("JINA_CLIP_DIM", str(embed.DEFAULT_DIM)))
    model = os.environ.get("JINA_CLIP_MODEL", embed.DEFAULT_MODEL)

    stats = run_index(
        client=client, milvus=milvus, store=store,
        jina_key=os.environ["JINA_API_KEY"],
        base_id=os.environ["KB_PRODUCTS_BASE_ID"],
        sheet_id=os.environ["KB_PRODUCTS_SHEET_ID"],
        dim=dim, model=model,
    )
    print(
        f"service=rag-index-products status=completed"
        f" records_read={stats['records_read']} products={stats['products']}"
        f" text={stats['text']} image={stats['image']}"
        f" image_failed={stats['image_failed']} written={stats['written']}"
    )


if __name__ == "__main__":
    main()
