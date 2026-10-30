# -*- coding: utf-8 -*-
"""rag-index-docs：钉钉知识库文档 → Milvus 向量索引管道（RAG 第二期）。

背景（2026-10-19）：第一期 rag-index-products 只把 AI 表商品（结构化字段 +
附件图片）打进 kb_rag_v1；业务文档（销售总结/规划、制度、手册一类）此前
落在 mart_ops.kb_doc_chunks，检索走纯 BM25 词面匹配——问「Q4 京东的打法是
什么」这类 paraphrase 语义问题召回不到。本模块把文档正文按标题结构切片后
写入**同一个 collection**，于是文档与商品图文共用同一向量空间、同一检索
入口（qa.retrieve 混排即可同时召回）。

数据源为什么是 JSON 清单而不是 OpenAPI：钉钉 doc/wiki API 至今未对本应用
授权（2026-10-10 实测 InvalidAction.NotFound），服务端读不到正文；沿用
kb_qa.sync_docs 既定的绕行链路——本机 dws CLI 导出 markdown → 清洗成
docs.json → 落到容器可见路径 → 本管道读清单灌库。API 权限开通后只需替换
「取正文」这一段，切片与幂等写入逻辑不动。

切片策略：
- 先按 markdown 标题层级切 section，每片带**标题路径**前缀（语义锚点，
  避免「}]$」式孤片答非所问）；
- 超长 section 再按字滑窗二次切（size=600 / overlap=80），末窗保留。

幂等：条目 id 包含内容、切片器和模型共同决定的版本；新版本完成
Embedding、数量/维度校验、写入和抽样检索后才清理旧版本，失败保留 last-good。

平台标准形态：
- 调度：scheduler 命令表 ``rag-index-docs``；
- 门禁：``require_business_run``（只读本地清单 + 写本地向量库，无外部调用）；
- 缺 key、缺输入、空/部分快照和索引失败均以非零退出，不登记正常成功。

环境变量（凭据进 /opt/dops/app.env，不入库）：
    RAG_EMBED_PROVIDER  ``jina``（默认，图文同空间）/ ``openai``（OpenAI 兼容
                        端点，含天翼 ai.ctaigw.cn 一类网关，纯文本）
    RAG_EMBED_API_KEY   凭据（缺省回读 JINA_API_KEY）
    RAG_EMBED_BASE_URL  provider=openai 必填，形如 https://host/v1
    RAG_EMBED_MODEL     模型名（换 provider 必须显式配）
    RAG_EMBED_DIM       维度（缺省回读 JINA_CLIP_DIM，再缺省 1024）
    RAG_MILVUS_PATH / RAG_DOCS_INPUT（清单路径，默认
    /opt/vector/data/kb_docs.json）

用法::

    python3 -m common.rag.index_docs --file /tmp/kb_docs.json --confirm-local-test-write

清单 JSON 形如::

    [{"doc_id": "amweZ92PV6wQbnYGHq5KD5mqJxEKBD6p",
      "doc_title": "京东Q3销售总结及Q4规划", "content": "# 摘要\\n..."}]
"""

import argparse
from datetime import datetime
import hashlib
import html
import json
import os
import re
from pathlib import Path

from common.rag import embed, freshness

#: 清单缺省路径：/opt/vector/data 与 rag.db 同区（scheduler 可写、recreate
#: 不丢；config-local 只读挂载，kb-sync-wiki 写不了——2026-10-21 冒烟实测）。
DEFAULT_INPUT = "/opt/vector/data/kb_docs.json"

#: 切片窗口与重叠（字）。600 字≈一段报告正文，80 字重叠防句义被腰斩。
CHUNK_SIZE = 600
CHUNK_OVERLAP = 80

#: 嵌入批大小（jina 单请求多条；过大易触发限流，过小浪费往返）。
TEXT_BATCH = 16

CHUNKER_VERSION = "markdown-section-v2"

#: doc_id 白名单：必须简洁可寻址，因为它直接进 Milvus 主键与 like 前缀，
#: 禁止空格/引号/百分号（like 通配符注入面）。
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: 钉钉导出正文里混的富文本标签：只剥标签保留文字（表格仍保持 markdown 行）。
_TAG_RE = re.compile(
    r"</?(?:span|div|font|p|br|strong|b|em|i|u|a|img|table|tbody|thead"
    r"|tr|td|th)\b[^>]*>",
    re.I,
)

#: 单条目文本上限：Milvus VARCHAR 长度墙（与 store._TEXT_LEN 同值，那边是
#: schema 真源）。这里独立常量是刻意的——index_docs 不 import store，避免
#: CLI 期被 pymilvus 拖进来；改长度时两处一起改（有测试钉住上限行为）。
TEXT_LIMIT = 2048

#: markdown 转义符（导出器为防渲染而加的反斜杠）。
_ESCAPE_RE = re.compile(r"\\([+\-*_#[\](){}])")

_BLANK_RE = re.compile(r"\n{3,}")

#: markdown 表格分隔行（``|---|---|``）：纯骨架零语义，被固定窗口切成独立
#: 残片时会霸占 top-k（BM25/向量皆然，实测「京东Q4打法」首条命中就是它）。
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def clean_markdown(text):
    """钉钉导出正文 → 纯文本：剥富文本标签、还原实体、去 markdown 转义。

    只处理导出污染，不动表格本身的 markdown 语法（``|`` 行列要留着，
    后续切片依赖表格语义密度）。
    """
    out = html.unescape(text or "")
    out = out.replace("\xa0", " ")
    out = _TAG_RE.sub("", out)
    out = _ESCAPE_RE.sub(r"\1", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    lines = [line for line in out.splitlines() if not _TABLE_RULE_RE.match(line)]
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def _strip_emphasis(title):
    """标题去掉 markdown 强调标记（钉钉导出通篇 ``**标题**``）。

    标题会作为每条切片的路径前缀进入 embedding，``**`` 这类符号是纯噪音。
    """
    return re.sub(r"[*`_]", "", title).strip()


def split_sections(text):
    """markdown → [(标题路径, 正文)]，按 ``#`` 层级维护路径栈。

    无标题文档返回 [("", 全文)]；只有标题没有正文的节被丢弃（父标题本身只
    做路径，正文挂在子节上）。
    """
    sections = []
    stack = {}
    current_path = ""
    buffer = []
    for line in (text or "").splitlines():
        matched = re.match(r"^(#{1,6})\s+(.*)$", line)
        if matched:
            if buffer and "".join(buffer).strip():
                sections.append((current_path, "\n".join(buffer).strip()))
            buffer = []
            level = len(matched.group(1))
            title = _strip_emphasis(matched.group(2))
            stack = {k: v for k, v in stack.items() if k < level}
            stack[level] = title
            levels = sorted(stack)
            current_path = " > ".join(stack[k] for k in levels)
            continue
        buffer.append(line)
    if buffer and "".join(buffer).strip():
        sections.append((current_path, "\n".join(buffer).strip()))
    if not sections and (text or "").strip():
        sections = [("", (text or "").strip())]
    return sections


def _slide(text, size, overlap):
    """按字滑窗切分单个 section（末窗不足也保留）。"""
    step = size - overlap
    pieces = []
    for start in range(0, len(text), step):
        piece = text[start:start + size].strip()
        if piece:
            pieces.append(piece)
        if start + size >= len(text):
            break
    return pieces


def build_chunks(content, *, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP,
                 text_limit=TEXT_LIMIT):
    """一篇文档正文 → 检索切片文本列表（带标题路径前缀）。

    每条不超 ``text_limit``（Milvus VARCHAR 硬性长度，超限写入直接报错）。
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")

    limit = text_limit or TEXT_LIMIT
    chunks = []
    for path, body in split_sections(clean_markdown(content)):
        prefix = f"[{path}] " if path else ""
        # 注意必须是列表：短原文直接成单片，写成 ``body`` 会被当字符序列迭代。
        pieces = [body] if len(body) <= size else _slide(body, size, overlap)
        for piece in pieces:
            chunk = (prefix + piece).strip()
            if not chunk:
                continue
            chunks.append(chunk if len(chunk) <= limit else chunk[:limit])
    return chunks


def normalize_doc(raw):
    """一条清单记录 → (doc_id, doc_title, content)；不合格返回 None。

    doc_id 不合 :data:`_DOC_ID_RE` 直接判废——它要进 Milvus 主键与 like
    前缀，宁可 skip 也不让任意字符串进过滤表达式。
    """
    if not isinstance(raw, dict):
        return None
    doc_id = str(raw.get("doc_id") or "").strip()
    content = raw.get("content")
    if not _DOC_ID_RE.match(doc_id):
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    title = raw.get("doc_title")
    title = str(title).strip() if title else ""
    return doc_id, title, content.strip()


def _chunk_id(doc_id, version, index):
    return f"doc:{doc_id}:{version}:{index:04d}"


def _doc_version(doc_id, title, texts, *, model, dim, size, overlap):
    digest = hashlib.sha256()
    for value in (doc_id, title, CHUNKER_VERSION, model, str(dim),
                  str(size), str(overlap), *texts):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:20]


def _snapshot_hash(payload):
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _error_label(exc):
    return str(exc) if isinstance(exc, embed.EmbedError) else type(exc).__name__


def run_index(payload, *, milvus, store, api_key, dim, model=embed.DEFAULT_MODEL,
              provider=embed.DEFAULT_PROVIDER, base_url=None,
              size=CHUNK_SIZE, overlap=CHUNK_OVERLAP, text_batch=TEXT_BATCH,
              on_progress=None, freshness_repository=None,
              source_id="wiki:kb_docs", source_snapshot_at=None,
              auto_delete=False, delete_max_count=100, delete_max_ratio=0.1):
    """完整准备并按版本发布文档；任何失败都保留可检索的旧版本。"""
    real_dim = store.ensure_collection(milvus, dim)
    snapshot_hash = _snapshot_hash(payload)
    stats = {
        "docs": 0, "chunks": 0, "skipped": 0, "written": 0,
        "unchanged": 0, "failed": 0, "deleted": 0, "delete_planned": 0,
        "source_count": 0, "source_complete": False, "status": "completed",
        "reason": "", "last_error": None, "content_hash": snapshot_hash,
        "index_version": snapshot_hash,
    }
    documents = []
    seen = set()
    for raw in payload:
        normalized = normalize_doc(raw)
        if normalized is None or normalized[0] in seen:
            stats["skipped"] += 1
            continue
        seen.add(normalized[0])
        documents.append(normalized)
    stats["source_count"] = len(documents)
    stats["source_complete"] = bool(documents) and stats["skipped"] == 0

    for doc_id, title, content in documents:
        texts = build_chunks(content, size=size, overlap=overlap)
        if not texts:
            stats["skipped"] += 1
            stats["source_complete"] = False
            continue
        version = _doc_version(
            doc_id, title, texts, model=model, dim=real_dim,
            size=size, overlap=overlap)
        version_prefix = f"doc:{doc_id}:{version}:"
        expected_ids = {
            _chunk_id(doc_id, version, index) for index in range(len(texts))
        }
        try:
            existing_ids = store.list_ids(milvus, f"doc:{doc_id}:")
            if expected_ids.issubset(existing_ids):
                stats["unchanged"] += 1
            else:
                vectors = []
                for start in range(0, len(texts), text_batch):
                    part = texts[start:start + text_batch]
                    embedded = embed.embed_items(
                        [{"text": text} for text in part], api_key,
                        model=model, dimensions=real_dim,
                        provider=provider, base_url=base_url)
                    vectors.extend(embed.validate_vectors(
                        embedded, len(part), dimensions=real_dim,
                        provider=provider))
                items = [
                    {
                        "id": _chunk_id(doc_id, version, index),
                        "vector": vector,
                        "kind": "text",
                        "name": title,
                        "text": text,
                        "image_ref": "",
                    }
                    for index, (text, vector) in enumerate(zip(texts, vectors))
                ]
                if len(items) != len(texts) or len({item["id"] for item in items}) != len(items):
                    raise embed.EmbedError("文档发布元数据或数量不符")
                written = store.upsert_items(milvus, items)
                if written != len(items):
                    raise RuntimeError("publish_count_mismatch")
                stats["written"] += written
                hits = store.search(
                    milvus, items[0]["vector"],
                    limit=max(1, min(20, len(items))), kind="text")
                if not any(str(hit.get("id", "")).startswith(version_prefix)
                           for hit in hits):
                    raise RuntimeError("sample_verification_failed")
            old_ids = [item_id for item_id in existing_ids
                       if not item_id.startswith(version_prefix)]
            if old_ids:
                stats["deleted"] += store.delete_ids(milvus, old_ids)
            stats["docs"] += 1
            stats["chunks"] += len(texts)
        except Exception as exc:
            stats["failed"] += 1
            stats["last_error"] = _error_label(exc)
        if on_progress:
            on_progress(doc_id, stats["chunks"])

    deletion_pending = 0
    stale_deleted = 0
    if stats["source_complete"] and stats["failed"] == 0:
        current_doc_ids = {doc_id for doc_id, _, _ in documents}
        all_ids = store.list_ids(milvus, "doc:")
        stale_ids = [item_id for item_id in all_ids
                     if item_id.split(":", 2)[1] not in current_doc_ids]
        stats["delete_planned"] = len(stale_ids)
        if stale_ids and auto_delete:
            previous_count = len(all_ids)
            ratio = len(stale_ids) / max(previous_count, 1)
            if (len(stale_ids) <= delete_max_count
                    and ratio <= delete_max_ratio):
                stale_deleted = store.delete_ids(milvus, stale_ids)
                stats["deleted"] += stale_deleted
            else:
                stats["reason"] = "delete_threshold"
        deletion_pending = stats["delete_planned"] - stale_deleted

    if not documents:
        stats["status"] = "skipped"
        stats["reason"] = "empty_source" if not payload else "incomplete_source"
    elif stats["failed"]:
        stats["status"] = "degraded"
        stats["reason"] = "index_failed"
    elif not stats["source_complete"]:
        stats["status"] = "degraded"
        stats["reason"] = "incomplete_source"
    elif deletion_pending:
        stats["status"] = "degraded"
        stats["reason"] = stats["reason"] or "deletion_pending"

    if freshness_repository:
        freshness_repository.record({
            "index_key": "kb_doc_vector",
            "source_type": "wiki",
            "source_id": source_id,
            "source_version": snapshot_hash,
            "content_hash": snapshot_hash,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": model,
            "dim": real_dim,
            "index_version": snapshot_hash,
            "source_count": stats["source_count"],
            "indexed_count": stats["docs"],
            "pending_count": stats["source_count"] - stats["docs"] + deletion_pending,
            "failed_count": stats["failed"],
            "last_source_snapshot_at": source_snapshot_at or datetime.now(),
            "status": "fresh" if stats["status"] == "completed" else (
                "blocked" if not documents else "degraded"),
            "last_error": stats["last_error"] or stats["reason"] or None,
        })
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rag.index_docs")
    parser.add_argument("--file", help=f"文档清单 JSON（默认 {DEFAULT_INPUT}）")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    provider = os.environ.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    path = Path(args.file or os.environ.get("RAG_DOCS_INPUT") or DEFAULT_INPUT)
    api_key = os.environ.get("RAG_EMBED_API_KEY") or os.environ.get("JINA_API_KEY")
    if not api_key:
        freshness.record_run_failure(
            ("kb_doc_vector",), source_type="wiki",
            source_id=f"wiki:{path.name}", reason="no_embed_key",
        )
        print("service=rag-index-docs status=skipped reason=no_embed_key")
        raise SystemExit(1)
    base_url = os.environ.get("RAG_EMBED_BASE_URL")
    if provider == embed.PROVIDER_OPENAI and not base_url:
        raise SystemExit("provider=openai 需要 RAG_EMBED_BASE_URL（如 https://host/v1）")
    if provider not in (embed.PROVIDER_JINA, embed.PROVIDER_OPENAI,
                        embed.PROVIDER_CTAIGW):
        raise SystemExit(f"未知 RAG_EMBED_PROVIDER: {provider}")

    if not path.is_file():
        freshness.record_run_failure(
            ("kb_doc_vector",), source_type="wiki",
            source_id=f"wiki:{path.name}", reason="no_input",
        )
        print(f"service=rag-index-docs status=skipped reason=no_input"
              f" file={path}")
        raise SystemExit(1)

    from common.public_data import db
    from common.public_data.freshness_repository import (
        KnowledgeIndexFreshnessRepository,
    )
    from common.public_data.live_safety import require_business_run
    from common.public_data.settings import Settings
    from common.rag import store

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("--file 必须是 JSON 数组")

    settings = Settings.from_environment()
    require_business_run(
        settings, confirm_local_test_write=args.confirm_local_test_write
    )
    milvus = store.connect(os.environ.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    dim = int(os.environ.get("RAG_EMBED_DIM")
              or os.environ.get("JINA_CLIP_DIM") or embed.DEFAULT_DIM)
    # 模型名：env 显式 > provider 默认（jina-clip-v2 / qwen3-vl-embedding）；
    # openai 无默认（jina-clip-v2 在别的网关上不存在，静默兜底只会换 403）。
    model = (os.environ.get("RAG_EMBED_MODEL") or os.environ.get("JINA_CLIP_MODEL")
             or embed.default_model(provider))
    if not model:
        raise SystemExit(f"provider={provider} 需要 RAG_EMBED_MODEL")

    text_batch = int(os.environ.get("RAG_EMBED_TEXT_BATCH", TEXT_BATCH))
    concurrency = int(os.environ.get("RAG_EMBED_CONCURRENCY", "1"))
    if not 1 <= text_batch <= 64 or concurrency != 1:
        raise SystemExit("RAG_EMBED_TEXT_BATCH 须为 1..64，且单写者并发只能为 1")
    state_connection = db.connect(settings.mart_database)
    try:
        stats = run_index(
            payload, milvus=milvus, store=store, api_key=api_key,
            dim=dim, model=model, provider=provider, base_url=base_url,
            text_batch=text_batch,
            freshness_repository=KnowledgeIndexFreshnessRepository(state_connection),
            source_id=f"wiki:{path.name}"[:128],
            source_snapshot_at=datetime.fromtimestamp(path.stat().st_mtime),
            auto_delete=os.environ.get("INDEX_AUTO_DELETE_ENABLE", "0") == "1",
            delete_max_count=int(os.environ.get("INDEX_DELETE_MAX_COUNT", "100")),
            delete_max_ratio=float(os.environ.get("INDEX_DELETE_MAX_RATIO", "0.1")),
        )
    finally:
        state_connection.close()
    reason = f" reason={stats['reason']}" if stats["reason"] else ""
    print(
        f"service=rag-index-docs status={stats['status']}{reason}"
        f" provider={provider} dim={dim}"
        f" docs={stats['docs']} chunks={stats['chunks']}"
        f" skipped={stats['skipped']} written={stats['written']}"
        f" unchanged={stats['unchanged']} failed={stats['failed']}"
        f" deleted={stats['deleted']} delete_planned={stats['delete_planned']}"
        f" index_version={stats['index_version']}"
    )
    if stats["status"] != "completed":
        raise SystemExit(1)
    return stats


if __name__ == "__main__":
    main()
