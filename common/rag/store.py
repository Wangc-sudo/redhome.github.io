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
    client.load_collection(COLLECTION)
    result = client.delete(COLLECTION, filter=f'id like "{prefix}%"')
    if isinstance(result, dict):
        return int(result.get("delete_count") or 0)
    return 0


def search(client, vector, *, limit=8, kind=None):
    """向量检索。kind="text"/"image" 限定模态，None 跨模态混排。

    返回 [{id, score, kind, name, text, image_ref},...]，score=COSINE 相似度。
    """
    client.load_collection(COLLECTION)
    flt = None
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
