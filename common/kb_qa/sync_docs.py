# -*- coding: utf-8 -*-
"""kb-sync-docs：产品手册 JSON → mart_ops.kb_doc_chunks 导入管道（P2）。

背景（2026-10-10 移交文档 §二）：钉钉 wiki/doc OpenAPI 未授权，服务端读不到
手册正文；采用绕行方案——本机 dws CLI 读 8 篇手册 → 生成 docs.json →
scp 上云 → 本 CLI 灌库。手册低频变更，接受人工触发；等 API 权限开通后
再复用本模块的切片与幂等写入逻辑接正式定时管道。

平台标准形态：
- 输入：JSON 文件，形如 ``[{"doc_id","doc_title","content"}, ...]``；
- 幂等：UNIQUE(doc_id, chunk_index) + ``INSERT ... ON DUPLICATE KEY UPDATE``
  （``AS new`` alias 形式，8.0.20+ 弃 ``VALUES()``）——重跑/重导不产生脏数据；
- 门禁：``require_business_run``（只读本地文件 + 写 mart_ops，不调外部）；
- 切片：按字固定窗口 :func:`chunk_text`（size=500 / overlap=50）；embedding
  列留空——P2 检索走纯 Python BM25，不生成向量。

用法::

    python3 -m common.kb_qa.sync_docs import \\
        --file /tmp/kb_docs.json --confirm-local-test-write
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

#: 切片窗口与重叠（字）。500 字约一段手册正文，50 字重叠防句义被腰斩。
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

_UPSERT_SQL = (
    "INSERT INTO `kb_doc_chunks`"
    " (`doc_id`, `doc_title`, `chunk_index`, `content`, `embedding`,"
    "  `synced_at`)"
    " VALUES (%s,%s,%s,%s,NULL,%s) AS new"
    " ON DUPLICATE KEY UPDATE"
    " `doc_title`=new.`doc_title`, `content`=new.`content`,"
    " `synced_at`=new.`synced_at`"
)


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """正文按字切固定窗口，返回切片列表；空白文本返回 []。

    末窗不足一定长度也保留（手册尾段常是关键结论，不能丢）。
    """
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")
    step = size - overlap
    chunks = []
    for start in range(0, len(text), step):
        chunk = text[start:start + size].strip()
        if chunk:
            chunks.append(chunk)
        if start + size >= len(text):
            break
    return chunks


def normalize_doc(raw):
    """一条 JSON 手册记录 → (doc_id, doc_title, content)；缺关键字段返回 None。"""
    if not isinstance(raw, dict):
        return None
    doc_id = str(raw.get("doc_id") or "").strip()
    content = raw.get("content")
    if not doc_id or not isinstance(content, str) or not content.strip():
        return None
    title = raw.get("doc_title")
    title = str(title).strip() if title else None
    return doc_id, title, content.strip()


def upsert_chunks(conn, doc_id, title, chunks, *, synced_at=None):
    """幂等写入一本手册的全部切片，返回写入条数。"""
    synced_at = synced_at or datetime.now()
    stamp = synced_at.strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.cursor()
    try:
        for index, content in enumerate(chunks):
            cursor.execute(_UPSERT_SQL, (doc_id, title, index, content, stamp))
    finally:
        cursor.close()
    return len(chunks)


def import_docs(conn, payload, *, synced_at=None):
    """JSON 记录列表 → kb_doc_chunks；返回 {docs, chunks, skipped} 统计。"""
    stats = {"docs": 0, "chunks": 0, "skipped": 0}
    for raw in payload:
        normalized = normalize_doc(raw)
        if normalized is None:
            stats["skipped"] += 1
            continue
        doc_id, title, content = normalized
        chunks = chunk_text(content)
        if not chunks:
            stats["skipped"] += 1
            continue
        stats["chunks"] += upsert_chunks(
            conn, doc_id, title, chunks, synced_at=synced_at
        )
        stats["docs"] += 1
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb_qa.sync_docs")
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import", help="从 JSON 文件导入手册切片")
    importer.add_argument("--file", required=True, help="docs.json 路径")
    importer.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("--file 必须是 JSON 数组")

    from common.public_data import db
    from common.public_data.live_safety import require_business_run
    from common.public_data.settings import Settings

    settings = Settings.from_environment()
    require_business_run(
        settings, confirm_local_test_write=args.confirm_local_test_write
    )

    conn = db.connect(settings.mart_database)
    try:
        stats = import_docs(conn, payload)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(
        f"service=kb-sync-docs status=completed docs={stats['docs']}"
        f" chunks={stats['chunks']} skipped={stats['skipped']}"
    )


if __name__ == "__main__":
    main()