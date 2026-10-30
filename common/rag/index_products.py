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
from datetime import datetime
import hashlib
import os
import time
import uuid

from common.kb_qa import media
from common.kb_qa.sync_products import ATTACHMENT_FIELDS, normalize_record
from common.rag import embed, freshness, index_state

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


def _new_stats():
    return {
        "records_read": 0, "products": 0, "text": 0, "image": 0,
        "image_failed": 0, "written": 0, "unchanged": 0,
        "text_embedded": 0, "image_embedded": 0, "deleted": 0,
        "failed": 0, "retried": 0, "api_calls": 0, "duration_ms": 0,
        "skipped": 0, "source_complete": False, "status": "completed",
        "reason": "", "delete_planned": 0, "publish_failed": 0,
        "last_error": None, "run_id": "", "index_version": "", "content_hash": "",
    }


def _descriptor(row, item_id, kind, content_hash, text, image_ref, model, dim):
    return {
        "item_id": item_id, "record_id": row["record_id"], "kind": kind,
        "content_hash": content_hash, "model": model, "dim": int(dim),
        "name": row["name"], "text": text, "image_ref": image_ref,
    }


def _is_unchanged(previous, item):
    """只有上次成功且身份、内容、模型和真实维度全一致才跳过 embedding。"""
    if not previous or previous.get("status") != index_state.STATUS_OK:
        return False
    try:
        previous_dim = int(previous.get("dim"))
    except (TypeError, ValueError):
        return False
    return (
        previous.get("item_id") == item["item_id"]
        and previous.get("content_hash") == item["content_hash"]
        and previous.get("model") == item["model"]
        and previous_dim == item["dim"]
    )


def _state_row(item, previous, *, status, now, unchanged=False, error=None):
    previous = previous or {}
    if status == index_state.STATUS_DEGRADED:
        attempts = int(previous.get("attempts") or 0) + 1
        last_success_at = previous.get("last_success_at")
    else:
        attempts = int(previous.get("attempts") or 0) if unchanged else 0
        last_success_at = previous.get("last_success_at") if unchanged else now
    return {
        key: item[key]
        for key in ("item_id", "record_id", "kind", "content_hash", "model", "dim")
    } | {
        "last_success_at": last_success_at,
        "status": status,
        "attempts": attempts,
        "last_error": error,
    }


def _milvus_item(item, vector):
    return {
        "id": item["item_id"], "vector": vector, "kind": item["kind"],
        "name": item["name"], "text": item["text"],
        "image_ref": item["image_ref"],
    }


def _error_label(exc):
    """EmbedError 已约定不含输入回显；其他异常只保留类型防签名 URL 泄露。"""
    return str(exc) if isinstance(exc, embed.EmbedError) else type(exc).__name__


def run_index(*, client, milvus, store, api_key, base_id, sheet_id, dim,
              model=embed.DEFAULT_MODEL, provider=embed.DEFAULT_PROVIDER,
              base_url=None, text_batch=None, image_batch=None, on_progress=None,
              state_repository=None, run_id=None, freshness_repository=None,
              auto_delete=False, delete_max_count=100, delete_max_ratio=0.1):
    """商品快照增量入库；失败保留 last-good，完整成功后才对账删除。

    文本哈希基于 normalize_record/build_text 后的规范化内容；图片哈希始终
    基于实际下载字节，签名 URL 只用于当次获取或 ctaigw embedding，不参与哈希。
    未传 state_repository 时保留旧的全量 embedding 行为，但不做失效清理。
    """
    started = time.perf_counter()
    run_id = run_id or uuid.uuid4().hex
    now = datetime.now()
    stats = _new_stats()
    stats["run_id"] = run_id
    stats["index_version"] = run_id
    text_batch = text_batch or embed.max_batch(provider)
    records = client.list_records(base_id, sheet_id) or []
    stats["records_read"] = len(records)
    pairs = []
    for record in records:
        row = normalize_record(record) if isinstance(record, dict) else None
        if row:
            pairs.append((row, record))
        else:
            stats["skipped"] += 1
    stats["products"] = len(pairs)
    stats["source_complete"] = bool(records) and stats["skipped"] == 0

    real_dim = store.ensure_collection(milvus, dim)
    previous = state_repository.preload() if state_repository else {}
    items = []
    state_rows = []

    def embed_part(inputs):
        stats["api_calls"] += 1
        vectors = embed.embed_items(
            inputs, api_key, model=model, dimensions=real_dim,
            provider=provider, base_url=base_url)
        # 再守一道入库边界，亦防止测试替身/未来 provider 绕过统一解析器。
        return embed.validate_vectors(
            vectors, len(inputs), dimensions=real_dim, provider=provider)

    def mark_failed(item, exc, *, image=False):
        stats["failed"] += 1
        if image:
            stats["image_failed"] += 1
        state_rows.append(_state_row(
            item, previous.get(item["item_id"]),
            status=index_state.STATUS_DEGRADED, now=now,
            error=_error_label(exc)))

    def mark_embedded(item, vector, *, image=False):
        items.append(_milvus_item(item, vector))
        if image:
            stats["image"] += 1
            stats["image_embedded"] += 1
        else:
            stats["text"] += 1
            stats["text_embedded"] += 1
        state_rows.append(_state_row(
            item, previous.get(item["item_id"]),
            status=index_state.STATUS_OK, now=now))

    pending_text = []
    for row, _ in pairs:
        text = build_text(row)
        item = _descriptor(
            row, f"product:{row['record_id']}", "text",
            hashlib.sha256(text.encode("utf-8")).hexdigest(), text, "",
            model, real_dim)
        old = previous.get(item["item_id"])
        if _is_unchanged(old, item):
            stats["unchanged"] += 1
            stats["text"] += 1
            state_rows.append(_state_row(
                item, old, status=index_state.STATUS_OK, now=now,
                unchanged=True))
        else:
            pending_text.append(item)

    for start in range(0, len(pending_text), text_batch):
        part = pending_text[start:start + text_batch]
        try:
            vectors = embed_part([{"text": item["text"]} for item in part])
        except embed.EmbedError as exc:
            for item in part:
                mark_failed(item, exc)
        else:
            for item, vector in zip(part, vectors):
                mark_embedded(item, vector)
        if on_progress:
            on_progress("text", stats["text"])

    def image_inputs(part):
        if provider == embed.PROVIDER_CTAIGW:
            return [{"image": meta["url"]} for _, meta, _ in part]
        return [
            {"image": embed.to_data_uri(content, meta["mime"])}
            for _, meta, content in part
        ]

    def process_images(part):
        try:
            vectors = embed_part(image_inputs(part))
        except embed.EmbedError as exc:
            if len(part) == 1:
                item, _, _ = part[0]
                mark_failed(item, exc, image=True)
            else:
                # 整批失败不能整批丢弃：拆成单图定位坏图，好图仍可更新。
                stats["retried"] += len(part)
                for item, meta, content in part:
                    single = [(item, meta, content)]
                    try:
                        vector = embed_part(image_inputs(single))[0]
                    except embed.EmbedError as single_exc:
                        mark_failed(item, single_exc, image=True)
                    else:
                        mark_embedded(item, vector, image=True)
        else:
            for (item, _, _), vector in zip(part, vectors):
                mark_embedded(item, vector, image=True)
        if on_progress:
            on_progress("image", stats["image"])
            on_progress("image_failed", stats["image_failed"])

    # 图片先下载并做字节 SHA256，再决定 unchanged；ctaigw 仍把签名 URL
    # 交给网关 embed。内存最多保留一个图片批次的字节，避免首轮全量占内存。
    pending_images = []
    image_batch = image_batch or embed.max_image_batch(provider)
    if not 1 <= image_batch <= embed.max_image_batch(provider):
        raise ValueError("image_batch exceeds provider limit")
    image_entries = [
        (row, field_name, index, meta)
        for row, record in pairs
        for field_name, index, meta in iter_image_attachments(record)
    ]
    for row, field_name, index, meta in image_entries:
        record_id = row["record_id"]
        item = _descriptor(
            row, f"product-img:{record_id}:{field_name}:{index}", "image",
            "", "", f"{record_id}/{field_name}/{index}", model, real_dim)
        old = previous.get(item["item_id"])
        try:
            content = media.download_bytes(meta["url"])
            item["content_hash"] = hashlib.sha256(content).hexdigest()
        except Exception as exc:
            item["content_hash"] = (old or {}).get("content_hash", "")
            mark_failed(item, exc, image=True)
            if on_progress:
                on_progress("image_failed", stats["image_failed"])
            continue
        if _is_unchanged(old, item):
            stats["unchanged"] += 1
            stats["image"] += 1
            state_rows.append(_state_row(
                item, old, status=index_state.STATUS_OK, now=now,
                unchanged=True))
            continue
        pending_images.append((item, meta, content))
        if len(pending_images) == image_batch:
            process_images(pending_images)
            pending_images = []
    if pending_images:
        process_images(pending_images)

    if items and state_repository:
        staged = []
        try:
            staged = store.stage_items(milvus, items, run_id=run_id)
            store.verify_staged_items(milvus, staged, dim=real_dim)
            stats["written"] = store.publish_staged_items(milvus, staged)
        except Exception as exc:
            try:
                store.discard_staged_items(milvus, staged)
            except Exception:
                pass
            published_ids = {item["id"] for item in items}
            stats["failed"] += len(items)
            stats["publish_failed"] = len(items)
            stats["last_error"] = _error_label(exc)
            for item in items:
                if item["kind"] == "image":
                    stats["image"] -= 1
                    stats["image_failed"] += 1
                else:
                    stats["text"] -= 1
            for row in state_rows:
                if row["item_id"] not in published_ids:
                    continue
                old = previous.get(row["item_id"]) or {}
                row["status"] = index_state.STATUS_DEGRADED
                row["attempts"] = int(old.get("attempts") or 0) + 1
                row["last_success_at"] = old.get("last_success_at")
                row["last_error"] = stats["last_error"]
    elif items:
        stats["written"] = store.upsert_items(milvus, items)
    complete = stats["source_complete"] and stats["failed"] == 0
    stale_ids = []
    deletion_pending = 0
    if state_repository:
        state_repository.save_batch(state_rows, run_id=run_id)
        stale_ids = state_repository.reconcile(run_id, complete=complete)
        stats["delete_planned"] = len(stale_ids)
        if stale_ids and auto_delete:
            ratio = len(stale_ids) / max(len(previous), 1)
            if (len(stale_ids) <= delete_max_count
                    and ratio <= delete_max_ratio):
                stats["deleted"] = store.delete_ids(milvus, stale_ids)
                state_repository.delete_ids(stale_ids)
            else:
                stats["reason"] = "delete_threshold"
        deletion_pending = len(stale_ids) - stats["deleted"]

    if not records:
        stats["status"] = "skipped"
        stats["reason"] = "empty_source"
    elif not stats["source_complete"]:
        stats["status"] = "degraded"
        stats["reason"] = "incomplete_source"
    elif stats["failed"]:
        stats["status"] = "degraded"
        stats["reason"] = "index_failed"
    elif deletion_pending:
        stats["status"] = "degraded"
        stats["reason"] = stats["reason"] or "deletion_pending"

    snapshot = hashlib.sha256()
    for row in sorted(state_rows, key=lambda value: value["item_id"]):
        snapshot.update(row["item_id"].encode("utf-8"))
        snapshot.update(b"\0")
        snapshot.update(row["content_hash"].encode("ascii"))
        snapshot.update(b"\0")
    stats["content_hash"] = snapshot.hexdigest()
    if freshness_repository:
        remaining_stale = stale_ids[stats["deleted"]:]
        text_pending = sum(not item_id.startswith("product-img:")
                           for item_id in remaining_stale)
        image_pending = len(remaining_stale) - text_pending
        common = {
            "source_type": "aitable",
            "source_id": f"{base_id}:{sheet_id}"[:128],
            "source_version": stats["content_hash"],
            "content_hash": stats["content_hash"],
            "chunker_version": "product-fields-v1",
            "embedding_model": model,
            "dim": real_dim,
            "index_version": run_id,
            "last_source_snapshot_at": now,
        }
        for index_key, kind, source_count, indexed_count, failed_count, pending in (
                ("kb_product_text_vector", "text", stats["products"],
                 stats["text"], stats["failed"] - stats["image_failed"],
                 text_pending),
                ("kb_product_image_vector", "image", len(image_entries),
                 stats["image"], stats["image_failed"], image_pending)):
            blocked = not records
            degraded = not stats["source_complete"] or failed_count or pending
            common_status = "blocked" if blocked else (
                "degraded" if degraded else "fresh")
            freshness_repository.record(common | {
                "index_key": index_key,
                "source_count": source_count,
                "indexed_count": indexed_count,
                "pending_count": pending + source_count - indexed_count - failed_count,
                "failed_count": failed_count,
                "status": common_status,
                "last_error": stats["last_error"] or stats["reason"] or None,
            })
    stats["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rag.index_products")
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    provider = os.environ.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = os.environ.get("RAG_EMBED_API_KEY") or os.environ.get("JINA_API_KEY")
    if not api_key:
        source_id = (
            f"{os.environ.get('KB_PRODUCTS_BASE_ID', 'unknown')}:"
            f"{os.environ.get('KB_PRODUCTS_SHEET_ID', 'unknown')}"
        )
        freshness.record_run_failure(
            ("kb_product_text_vector", "kb_product_image_vector"),
            source_type="aitable", source_id=source_id, reason="no_embed_key",
        )
        stats = _new_stats()
        stats.update(status="skipped", reason="no_embed_key")
        print("service=rag-index-products status=skipped reason=no_embed_key")
        raise SystemExit(1)
    base_url = os.environ.get("RAG_EMBED_BASE_URL")

    from common.dingtalk.client import DingTalkClient
    from common.public_data import db
    from common.public_data.freshness_repository import (
        KnowledgeIndexFreshnessRepository,
    )
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

    text_batch = int(os.environ.get("RAG_EMBED_TEXT_BATCH", embed.max_batch(provider)))
    image_batch = int(os.environ.get("RAG_EMBED_IMAGE_BATCH", embed.max_image_batch(provider)))
    concurrency = int(os.environ.get("RAG_EMBED_CONCURRENCY", "1"))
    if not 1 <= text_batch <= 64 or concurrency != 1:
        raise SystemExit("RAG_EMBED_TEXT_BATCH 须为 1..64，且单写者并发只能为 1")
    state_connection = db.connect(settings.mart_database)
    try:
        stats = run_index(
            client=client, milvus=milvus, store=store,
            api_key=api_key,
            base_id=os.environ["KB_PRODUCTS_BASE_ID"],
            sheet_id=os.environ["KB_PRODUCTS_SHEET_ID"],
            dim=dim, model=model, provider=provider, base_url=base_url,
            text_batch=text_batch, image_batch=image_batch,
            state_repository=index_state.RagIndexStateRepository(state_connection),
            freshness_repository=KnowledgeIndexFreshnessRepository(state_connection),
            auto_delete=os.environ.get("INDEX_AUTO_DELETE_ENABLE", "0") == "1",
            delete_max_count=int(os.environ.get("INDEX_DELETE_MAX_COUNT", "100")),
            delete_max_ratio=float(os.environ.get("INDEX_DELETE_MAX_RATIO", "0.1")),
            # 全量灌库可能跑几十分钟，无进度输出时无法区分「慢」与「卡死」
            # （2026-10-20 两次排查教训）：每批落一行进度。
            on_progress=lambda kind, n: print(f"progress {kind}={n}", flush=True),
        )
    finally:
        state_connection.close()
    reason = f" reason={stats['reason']}" if stats["reason"] else ""
    print(
        f"service=rag-index-products status={stats['status']}{reason}"
        f" records_read={stats['records_read']} products={stats['products']}"
        f" text={stats['text']} image={stats['image']}"
        f" image_failed={stats['image_failed']} written={stats['written']}"
        f" unchanged={stats['unchanged']}"
        f" text_embedded={stats['text_embedded']}"
        f" image_embedded={stats['image_embedded']}"
        f" deleted={stats['deleted']} delete_planned={stats['delete_planned']}"
        f" failed={stats['failed']} publish_failed={stats['publish_failed']}"
        f" retried={stats['retried']}"
        f" api_calls={stats['api_calls']} duration_ms={stats['duration_ms']}"
        f" index_version={stats['index_version']}"
    )
    if stats["status"] != "completed":
        raise SystemExit(1)
    return stats


if __name__ == "__main__":
    main()
