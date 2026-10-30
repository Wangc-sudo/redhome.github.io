# -*- coding: utf-8 -*-
"""Milvus Lite 向量库读写：单 collection 图文同空间（方案 A）。

API 契约以 dops-ci 实测冒烟（scripts/vector_smoke.py）为准：
- MilvusClient(path) 本地嵌入式；collection 建库后 dim 不可改；
- 建索引 client.prepare_index_params().add_index(index_type="AUTOINDEX", metric_type="COSINE")；
- search 返回 [[{"id","distance","entity"},...]]；query 用 filter 字符串标量过滤。

条目 id 约定（幂等 upsert 键）：
    商品文本  product:{record_id}
    商品图片  product-img:{record_id}:{字段名}:{序号}
    文档切片  doc:{doc_id}:{切片序号:04d}
商品下架/图片更换时按 id 先 delete 再 insert（幂等重跑无脏数据）；
文档改版时按 ``doc:{doc_id}:`` 前缀先清空（切片数会变，逐个删会留残块）。
"""

from pymilvus import DataType, MilvusClient

COLLECTION = "kb_rag_v1"
DEFAULT_PATH = "/opt/vector/data/rag.db"

# 字段长度上限：name 中文商品名 <255；image_ref 记「record_id/字段/序号」。
_ID_LEN = 128
_TEXT_LEN = 2048
_NAME_LEN = 255
_KIND_LEN = 16
_FIELD_LEN = 64


def connect(path=DEFAULT_PATH):
    """Milvus Lite 客户端（本地文件，无需起服务）。"""
    return MilvusClient(path)


def _schema(client, dim):
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=_ID_LEN)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("kind", DataType.VARCHAR, max_length=_KIND_LEN)  # text / image
    schema.add_field("name", DataType.VARCHAR, max_length=_NAME_LEN)  # 商品名
    schema.add_field("text", DataType.VARCHAR, max_length=_TEXT_LEN)  # 文本条目内容
    schema.add_field("image_ref", DataType.VARCHAR, max_length=_ID_LEN)  # 图片条目回指
    return schema


def ensure_collection(client, dim):
    """幂等建库：不存在则建（dim 一经确定不可改），存在则校验 dim 一致。

    返回真实维度（建库后以此为准；与 env JINA_CLIP_DIM 不一致时以库为准）。
    """
    if client.has_collection(COLLECTION):
        info = client.describe_collection(COLLECTION)
        existing = _dim_of(info)
        if existing is not None and existing != dim:
            raise RuntimeError(
                f"collection {COLLECTION} 已存在 dim={existing}，与期望 {dim} 不符"
                "（dim 建库后不可改，需先 drop_collection 重建）"
            )
        return existing or dim
    client.create_collection(collection_name=COLLECTION, schema=_schema(client, dim))
    idx = client.prepare_index_params()
    idx.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
    client.create_index(COLLECTION, index_params=idx)
    return dim


def _dim_of(info):
    """从 describe_collection 解析 vector 维度。"""
    for field in (info or {}).get("fields", []):
        if field.get("name") == "vector":
            params = field.get("params") or {}
            dim = params.get("dim")
            return int(dim) if dim else None
    return None


def upsert_items(client, items, *, batch=64):
    """幂等写入：先按 id delete 再 insert。返回写入条数。

    items=[{"id","vector","kind","name","text","image_ref"},...]
    """
    if not items:
        return 0
    client.load_collection(COLLECTION)
    written = 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        ids = [it["id"] for it in chunk]
        client.delete(COLLECTION, ids=ids)
        client.insert(COLLECTION, chunk)
        written += len(chunk)
    return written


def stage_items(client, items, *, run_id, batch=64):
    """将候选新向量写入不参与检索的 staging ID，生产 ID 暂不动。"""
    import hashlib

    staged = []
    for item in items:
        digest = hashlib.sha256(
            f"{run_id}\0{item['id']}".encode("utf-8")
        ).hexdigest()[:40]
        staged_id = f"__stage__:{digest}"
        staged.append({"staged_id": staged_id, "item": item})
    client.load_collection(COLLECTION)
    for start in range(0, len(staged), batch):
        part = staged[start:start + batch]
        rows = [entry["item"] | {
            "id": entry["staged_id"], "kind": "staging",
        } for entry in part]
        client.upsert(COLLECTION, rows)
    return staged


def verify_staged_items(client, staged, *, dim, batch=256):
    """核对暂存数量、向量维度/有限值，并抽样确认新 ID 可检索。"""
    import math

    if not staged:
        return True
    expected = {entry["staged_id"] for entry in staged}
    found = set()
    client.load_collection(COLLECTION)
    ids = sorted(expected)
    for start in range(0, len(ids), batch):
        part = ids[start:start + batch]
        values = ", ".join(f'"{item_id}"' for item_id in part)
        rows = client.query(
            COLLECTION, filter=f"id in [{values}]", output_fields=["id"],
            limit=len(part),
        )
        found.update(row["id"] for row in rows)
    if found != expected:
        raise RuntimeError("staging_count_mismatch")
    for entry in staged:
        vector = entry["item"].get("vector") or []
        if len(vector) != dim or not all(math.isfinite(value) for value in vector):
            raise RuntimeError("staging_vector_invalid")
    sample_limit = max(1, min(20, len(staged)))
    hits = search(
        client, staged[0]["item"]["vector"], limit=sample_limit, kind="staging",
    )
    if not any(hit.get("id") in expected for hit in hits):
        raise RuntimeError("staging_sample_verification_failed")
    return True


def _query_rows(client, ids, output_fields, *, batch=256):
    rows = []
    ids = list(dict.fromkeys(ids))
    for start in range(0, len(ids), batch):
        part = ids[start:start + batch]
        values = ", ".join(f'"{item_id}"' for item_id in part)
        rows.extend(client.query(
            COLLECTION, filter=f"id in [{values}]",
            output_fields=output_fields, limit=len(part),
        ))
    return rows


def _restore_production(client, previous, published_ids):
    restore = [previous[item_id] for item_id in published_ids if item_id in previous]
    if restore:
        client.upsert(COLLECTION, restore)
    inserted = [item_id for item_id in published_ids if item_id not in previous]
    if inserted:
        delete_ids(client, inserted)


def publish_staged_items(client, staged, *, batch=64):
    """发布后再次验收；中途失败时恢复本轮涉及的生产 last-good。"""
    if not staged:
        return 0
    client.load_collection(COLLECTION)
    fields = ["id", "vector", "kind", "name", "text", "image_ref"]
    production_ids = [entry["item"]["id"] for entry in staged]
    previous = {
        row["id"]: row
        for row in _query_rows(client, production_ids, fields)
    }
    published_ids = []
    try:
        for start in range(0, len(staged), batch):
            part = staged[start:start + batch]
            result = client.upsert(COLLECTION, [entry["item"] for entry in part])
            if isinstance(result, dict) and "upsert_count" in result:
                count = int(result["upsert_count"] or 0)
                if count != len(part):
                    raise RuntimeError("publish_count_mismatch")
            published_ids.extend(entry["item"]["id"] for entry in part)
        found = {
            row["id"] for row in _query_rows(client, production_ids, ["id"])
        }
        if found != set(production_ids):
            raise RuntimeError("publish_count_mismatch")
        expected = set(production_ids)
        first = staged[0]["item"]
        hits = search(
            client, first["vector"], limit=max(1, min(20, len(staged))),
            kind=first["kind"],
        )
        if not any(hit.get("id") in expected for hit in hits):
            raise RuntimeError("publish_sample_verification_failed")
    except Exception:
        try:
            _restore_production(client, previous, published_ids)
        except Exception as rollback_exc:
            raise RuntimeError("publish_rollback_failed") from rollback_exc
        raise
    delete_ids(client, [entry["staged_id"] for entry in staged])
    return len(published_ids)


def discard_staged_items(client, staged):
    if staged:
        return delete_ids(client, [entry["staged_id"] for entry in staged])
    return 0


def delete_ids(client, ids, *, batch=256):
    """按主键批量删除失效向量；只接受 ID 列表，不拼过滤表达式。"""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return 0
    client.load_collection(COLLECTION)
    deleted = 0
    for start in range(0, len(ids), batch):
        part = ids[start:start + batch]
        result = client.delete(COLLECTION, ids=part)
        if isinstance(result, dict):
            deleted += int(result.get("delete_count") or 0)
        else:
            deleted += len(part)
    return deleted


def delete_prefix(client, prefix):
    """按 id 前缀清空条目，返回删除条数（文档重导入前用）。

    Milvus 标量过滤支持 ``id like "前缀%"``。prefix 由调用方约束成安全
    字符集（见 index_docs._DOC_ID_RE），不拼任何用户自由文本。
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    client.load_collection(COLLECTION)
    result = client.delete(COLLECTION, filter=f'id like "{escaped}%"')
    if isinstance(result, dict):
        return int(result.get("delete_count") or 0)
    return 0


def list_ids(client, prefix, *, batch=1000):
    """分页列出一个安全前缀下的主键，供版本切换和删除计划使用。"""
    if not prefix or any(char in prefix for char in ('"', "\n", "\r")):
        raise ValueError("invalid id prefix")
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    client.load_collection(COLLECTION)
    ids = []
    offset = 0
    while True:
        rows = client.query(
            COLLECTION,
            filter=f'id like "{escaped}%"',
            output_fields=["id"],
            limit=batch,
            offset=offset,
        )
        ids.extend(row["id"] for row in rows)
        if len(rows) < batch:
            return ids
        offset += len(rows)


def search(client, vector, *, limit=8, kind=None):
    """向量检索。kind="text"/"image" 限定模态，None 跨模态混排。

    返回 [{id, score, kind, name, text, image_ref},...]，score=COSINE 相似度。
    """
    client.load_collection(COLLECTION)
    flt = 'kind != "staging"'
    if kind:
        flt = f'kind == "{kind}"'
    res = client.search(
        COLLECTION, [vector], limit=limit, filter=flt,
        output_fields=["kind", "name", "text", "image_ref"],
    )
    hits = []
    for hit in res[0]:
        ent = hit.get("entity") or {}
        hits.append({
            "id": hit.get("id"),
            "score": hit.get("distance"),
            "kind": ent.get("kind"),
            "name": ent.get("name"),
            "text": ent.get("text"),
            "image_ref": ent.get("image_ref"),
        })
    return hits


def count(client):
    """collection 当前条目数（巡检用）。"""
    client.load_collection(COLLECTION)
    res = client.query(COLLECTION, filter="id != ''", output_fields=["count(*)"])
    if res and isinstance(res, list):
        return int(res[0].get("count(*)", 0))
    return 0
