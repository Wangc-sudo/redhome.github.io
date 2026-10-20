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
    RAG_EMBED_PROVIDER  ``jina``（默认，图文同空间）/ ``ctaigw``（天翼网关
                        qwen3-vl-embedding，图文同空间 2560 维）/ ``openai``
    RAG_EMBED_API_KEY   凭据（缺省回读 JINA_API_KEY）
    RAG_EMBED_BASE_URL / RAG_EMBED_MODEL / RAG_EMBED_DIM（维度回读
    JINA_CLIP_DIM；ctaigw 固定 2560，dimensions 参数不下发）
    RAG_MILVUS_PATH
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


def run_index(*, client, milvus, store, api_key, base_id, sheet_id, dim,
              model=embed.DEFAULT_MODEL, provider=embed.DEFAULT_PROVIDER,
              base_url=None, text_batch=64, on_progress=None):
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
        vectors = embed.embed_items([{"text": t} for t in texts], api_key,
                                    model=model, dimensions=real_dim,
                                    provider=provider, base_url=base_url)
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
            if provider == embed.PROVIDER_CTAIGW:
                # 天翼网关支持 http(s) 图片 URL（实测）；钉钉附件签名 URL 带
                # 时效，刚好即用即弃，免下载字节流。data URI 形态未经其实测，
                # 不赌。
                vectors = embed.embed_items(
                    [{"image": meta["url"]} for _, _, meta in images],
                    api_key, model=model, dimensions=real_dim,
                    provider=provider, base_url=base_url)
            else:
                contents = [media.download_bytes(meta["url"])
                            for _, _, meta in images]
                vectors = embed.embed_items(
                    [{"image": embed.to_data_uri(content, meta["mime"])}
                     for content, (_, _, meta) in zip(contents, images)],
                    api_key, model=model, dimensions=real_dim,
                    provider=provider, base_url=base_url)
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

    # fail-soft：凭据未配置不算故障（灰度期 seed enabled 但 env 未发 key 时
    # 每小时静默跳过，不刷失败告警）；配好即自动生效。
    provider = os.environ.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = os.environ.get("RAG_EMBED_API_KEY") or os.environ.get("JINA_API_KEY")
    if not api_key:
        print("service=rag-index-products status=skipped reason=no_embed_key")
        return
    base_url = os.environ.get("RAG_EMBED_BASE_URL")

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
    dim = int(os.environ.get("RAG_EMBED_DIM")
              or os.environ.get("JINA_CLIP_DIM") or embed.DEFAULT_DIM)
    model = (os.environ.get("RAG_EMBED_MODEL")
             or os.environ.get("JINA_CLIP_MODEL")
             or embed.default_model(provider))
    if not model:
        raise SystemExit(f"provider={provider} 需要 RAG_EMBED_MODEL")

    stats = run_index(
        client=client, milvus=milvus, store=store,
        api_key=api_key,
        base_id=os.environ["KB_PRODUCTS_BASE_ID"],
        sheet_id=os.environ["KB_PRODUCTS_SHEET_ID"],
        dim=dim, model=model, provider=provider, base_url=base_url,
    )
    print(
        f"service=rag-index-products status=completed"
        f" records_read={stats['records_read']} products={stats['products']}"
        f" text={stats['text']} image={stats['image']}"
        f" image_failed={stats['image_failed']} written={stats['written']}"
    )


if __name__ == "__main__":
    main()
