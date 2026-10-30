# -*- coding: utf-8 -*-
"""kb-sync-wiki：钉钉知识库（wiki）→ 文档清单 + BM25 切片表。

背景（2026-10-21）：wiki/doc OpenAPI 权限已批（知识库读 / 知识库节点读 /
企业存储文件读），替代「本机 dws 导出 + 手动灌库」绕行（见 sync_docs
模块 docstring 的历史说明）。文档改版后每日自动进两条检索通路。

链路：
    wiki workspaces（operatorId 可见的知识库，可用 KB_WIKI_WORKSPACE_IDS
    逗号分隔白名单收窄）
      → workspace 详情拿 rootNodeId → nodes 递归遍历（v2.0）
      → 只取 category=ALIDOC 且扩展名 .adoc 的文档节点（表格 .axls/.able
        没有正文 API，跳过）
      → doc suites blocks 分页拉取（/v1.0/doc/suites/documents/{nodeId}）
      → blocks_to_markdown（heading→# 层级 / paragraph / 列表 / table→
        markdown 表格 / blockquote；unknown（hr 等）跳过）
      → 双写：
        1. 清单 JSON（KB_DOCS_OUTPUT，rag-index-docs 每日 03:17 读它灌向量库）；
        2. kb_doc_chunks（BM25 通路；按 doc_id 先 DELETE 再灌——切片数随
           改版变化，UPSERT 会留孤儿尾块）。

幂等：整体可重跑。单篇失败或空快照会拒绝整批发布并保留旧版本；完整快照
先提交 BM25，再原子替换清单。源删除默认只生成计划，受开关与双阈值约束。
门禁：require_live_run（外部 live 读钉钉 API + 写 mart_ops + 写清单文件）。

环境变量：
    KB_DINGTALK_APP_KEY / KB_DINGTALK_APP_SECRET / KB_DINGTALK_OPERATOR_ID
    KB_WIKI_WORKSPACE_IDS   可选，逗号分隔 workspaceId 白名单（默认全部可见）
    KB_DOCS_OUTPUT          清单输出路径（默认
                            /opt/dops/config-local/kb_docs.json）
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

from common.kb_qa.sync_docs import chunk_text, upsert_chunks

#: 知识库文档类别与扩展名（节点上还有 .axls/.able 表格等，无正文 API）。
_DOC_CATEGORY = "ALIDOC"
_DOC_EXTENSION = ".adoc"

#: blocks 分页大小（实测 size=100 单页即返回全部 248 块，仍按 nextToken 翻页）。
_PAGE_SIZE = 100

#: 清单默认输出路径（与 rag.index_docs.DEFAULT_INPUT 对齐）。
#: config-local 是只读挂载写不了（2026-10-21 冒烟 OSError 实测）；
#: /opt/vector/data 与 rag.db 同区：scheduler 可写、容器 recreate 不丢。
DEFAULT_OUTPUT = "/opt/vector/data/kb_docs.json"


class SourceSnapshotError(RuntimeError):
    pass


def blocks_to_markdown(blocks):
    """doc suites blocks → markdown 文本。

    blocks 是扁平有序列表（index 递增），heading.level 形如
    "heading-1"。输出保持文档顺序；空文本块丢弃。
    """
    lines = []
    for block in sorted(blocks, key=lambda b: b.get("index", 0)):
        block_type = block.get("blockType")
        body = block.get(block_type) if block_type else None
        if not isinstance(body, dict):
            continue
        text = (body.get("text") or "").strip()
        if block_type == "heading":
            level = str(body.get("level") or "heading-1")
            depth = level.rsplit("-", 1)[-1]
            marks = "#" * max(1, min(int(depth) if depth.isdigit() else 1, 6))
            if text:
                lines.append(f"{marks} {text}")
        elif block_type == "paragraph":
            if text:
                lines.append(text)
        elif block_type == "unorderedList":
            if text:
                lines.append(f"- {text}")
        elif block_type == "orderedList":
            if text:
                lines.append(f"1. {text}")
        elif block_type == "blockquote":
            if text:
                lines.append(f"> {text}")
        elif block_type == "table":
            rows = body.get("cells") or []
            table_lines = _table_to_markdown(rows)
            lines.extend(table_lines)
        # unknown（hr 等）与其他类型：跳过
    return "\n\n".join(lines)


def _table_to_markdown(rows):
    """table.cells（二维数组）→ markdown 表格行；空表返回 []。"""
    rows = [[str(cell or "").replace("|", "\\|").replace("\n", " ")
             for cell in row] for row in rows]
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "---|" * width]
    for row in rows[1:]:
        out.append("| " + " | ".join(row) + " |")
    return out


class WikiClient:
    """wiki/doc 读取封装：复用 DingTalkClient 的 token 管理与 _http_json。"""

    def __init__(self, client, operator_id):
        self._client = client
        self._operator_id = operator_id

    def _get(self, url):
        from common.dingtalk.client import _http_json

        headers = {"x-acs-dingtalk-access-token": self._client.get_token()}
        return _http_json(url, headers=headers)

    def workspaces(self):
        """operatorId 可见的知识库 → [(workspaceId, name)]。"""
        data = self._get(
            "https://api.dingtalk.com/v2.0/wiki/workspaces"
            f"?operatorId={self._operator_id}")
        items = data.get("value", data.get("workspaces", []))
        return [(ws.get("workspaceId"), ws.get("name") or "")
                for ws in items if ws.get("workspaceId")]

    def root_node(self, workspace_id):
        data = self._get(
            "https://api.dingtalk.com/v2.0/wiki/workspaces/"
            f"{workspace_id}?operatorId={self._operator_id}")
        workspace = data.get("workspace", data.get("value", data))
        return workspace.get("rootNodeId")

    def children(self, workspace_id, parent_node_id):
        """某节点的子节点列表。"""
        data = self._get(
            f"https://api.dingtalk.com/v2.0/wiki/nodes"
            f"?operatorId={self._operator_id}&workspaceId={workspace_id}"
            f"&parentNodeId={parent_node_id}&maxResults=50")
        return data.get("value", data.get("nodes", []))

    def iter_documents(self, workspace_id):
        """递归遍历知识库，产出文档节点（nodeId, title）。"""
        root = self.root_node(workspace_id)
        if not root:
            return
        stack = [root]
        while stack:
            parent = stack.pop()
            for node in self.children(workspace_id, parent):
                if node.get("hasChildren"):
                    stack.append(node.get("nodeId"))
                name = node.get("name") or ""
                if (node.get("category") == _DOC_CATEGORY
                        and name.endswith(_DOC_EXTENSION)):
                    yield node.get("nodeId"), name[:-len(_DOC_EXTENSION)]

    def document_blocks(self, node_id):
        """分页拉取一个文档的全部 blocks。"""
        blocks, token = [], ""
        while True:
            url = ("https://api.dingtalk.com/v1.0/doc/suites/documents/"
                   f"{node_id}/blocks?operatorId={self._operator_id}"
                   f"&size={_PAGE_SIZE}")
            if token:
                url += f"&nextToken={token}"
            data = self._get(url)
            result = data.get("result", data)
            blocks.extend(result.get("data", result.get("blocks", [])))
            token = result.get("nextToken")
            if not token:
                return blocks


def delete_doc_chunks(conn, doc_id):
    """按 doc_id 清空 BM25 切片（重建前调用），返回删除条数。"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM `kb_doc_chunks` WHERE `doc_id`=%s", (doc_id,))
        return cursor.rowcount
    finally:
        cursor.close()


def collect_documents(wiki, workspace_ids=None, on_progress=None):
    """全部目标知识库 → [{doc_id, doc_title, content}]，单篇失败跳过。"""
    workspaces = wiki.workspaces()
    if workspace_ids:
        allow = set(workspace_ids)
        workspaces = [ws for ws in workspaces if ws[0] in allow]
    docs = []
    stats = {"workspaces": len(workspaces), "failed": 0}
    for workspace_id, ws_name in workspaces:
        for node_id, title in wiki.iter_documents(workspace_id):
            try:
                blocks = wiki.document_blocks(node_id)
                content = blocks_to_markdown(blocks)
            except Exception:
                stats["failed"] += 1
                if on_progress:
                    on_progress(f"failed {title}")
                continue
            if not content.strip():
                stats["failed"] += 1
                continue
            docs.append({"doc_id": node_id, "doc_title": title,
                         "content": content, "workspace": ws_name})
            if on_progress:
                on_progress(f"doc {title} blocks={len(blocks)}")
    return docs, stats


def list_indexed_doc_ids(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT `doc_id` FROM `kb_doc_chunks`")
        return {row["doc_id"] if isinstance(row, dict) else row[0]
                for row in cursor.fetchall()}
    finally:
        cursor.close()


def _snapshot_hash(payload):
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _record_freshness(repository, stats, *, source_id, status, error=None):
    if not repository:
        return
    repository.record({
        "index_key": "kb_doc_bm25",
        "source_type": "wiki",
        "source_id": source_id,
        "source_version": stats["content_hash"],
        "content_hash": stats["content_hash"],
        "chunker_version": "kb-bm25-v1",
        "index_version": stats["index_version"],
        "source_count": stats.get("source_count", 0),
        "indexed_count": stats.get("docs", 0),
        "pending_count": stats.get("deletion_pending", 0),
        "failed_count": stats.get("failed", 0),
        "last_source_snapshot_at": datetime.now(),
        "status": status,
        "last_error": error,
    })


def run_sync(*, wiki, conn, out_path, workspace_ids=None, on_progress=None,
             freshness_repository=None, source_id="wiki:all-visible",
             auto_delete=False, delete_max_count=100, delete_max_ratio=0.1):
    """只发布完整快照；BM25 提交后原子替换清单，失败保留下载旧版本。"""
    docs, stats = collect_documents(wiki, workspace_ids=workspace_ids,
                                    on_progress=on_progress)
    payload = [{"doc_id": d["doc_id"], "doc_title": d["doc_title"],
                "content": d["content"]} for d in docs]
    stats.update({
        "docs": 0, "chunks": 0, "deleted": 0, "delete_planned": 0,
        "source_count": len(payload), "source_complete": bool(payload)
        and stats["failed"] == 0, "content_hash": _snapshot_hash(payload),
        "index_version": _snapshot_hash(payload), "status": "completed",
        "reason": "", "deletion_pending": 0,
    })
    if not stats["source_complete"]:
        reason = "empty_source" if not payload else "incomplete_source"
        stats.update(status="blocked" if not payload else "degraded",
                     reason=reason)
        _record_freshness(
            freshness_repository, stats, source_id=source_id,
            status=stats["status"], error=reason)
        raise SourceSnapshotError(reason)

    prepared = []
    for doc in payload:
        chunks = chunk_text(doc["content"])
        if not chunks:
            stats.update(status="degraded", reason="empty_chunks")
            _record_freshness(
                freshness_repository, stats, source_id=source_id,
                status="degraded", error="empty_chunks")
            raise SourceSnapshotError("empty_chunks")
        prepared.append((doc, chunks))

    current_ids = {doc["doc_id"] for doc in payload}
    stale_ids = sorted(list_indexed_doc_ids(conn) - current_ids)
    stats["delete_planned"] = len(stale_ids)
    delete_stale = False
    if stale_ids and auto_delete:
        ratio = len(stale_ids) / max(len(stale_ids) + len(current_ids), 1)
        delete_stale = (len(stale_ids) <= delete_max_count
                        and ratio <= delete_max_ratio)
        if not delete_stale:
            stats["reason"] = "delete_threshold"

    out = Path(out_path)
    temporary = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        for doc, chunks in prepared:
            delete_doc_chunks(conn, doc["doc_id"])
            stats["chunks"] += upsert_chunks(
                conn, doc["doc_id"], doc["doc_title"], chunks)
            stats["docs"] += 1
        if delete_stale:
            for doc_id in stale_ids:
                stats["deleted"] += delete_doc_chunks(conn, doc_id)
        conn.commit()
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, out)
    except Exception as exc:
        conn.rollback()
        temporary.unlink(missing_ok=True)
        _record_freshness(
            freshness_repository, stats, source_id=source_id,
            status="degraded", error=type(exc).__name__)
        raise

    stats["deletion_pending"] = 0 if delete_stale else len(stale_ids)
    if stats["deletion_pending"]:
        stats["status"] = "degraded"
        stats["reason"] = stats["reason"] or "deletion_pending"
    _record_freshness(
        freshness_repository, stats, source_id=source_id,
        status="fresh" if stats["status"] == "completed" else "degraded",
        error=stats["reason"] or None)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb_qa.sync_wiki")
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    parser.add_argument("--output", help=f"清单输出路径（默认 {DEFAULT_OUTPUT}）")
    args = parser.parse_args(argv)

    from common.dingtalk.client import DingTalkClient
    from common.public_data import db
    from common.public_data.freshness_repository import (
        KnowledgeIndexFreshnessRepository,
    )
    from common.public_data.live_safety import require_live_run
    from common.public_data.settings import Settings

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
    wiki = WikiClient(client, os.environ["KB_DINGTALK_OPERATOR_ID"])
    workspace_ids = [item.strip() for item in
                     os.environ.get("KB_WIKI_WORKSPACE_IDS", "").split(",")
                     if item.strip()] or None
    out_path = args.output or os.environ.get("KB_DOCS_OUTPUT") or DEFAULT_OUTPUT

    conn = db.connect(settings.mart_database)
    try:
        stats = run_sync(
            wiki=wiki, conn=conn, out_path=out_path,
            workspace_ids=workspace_ids,
            on_progress=lambda msg: print(f"progress {msg}", flush=True),
            freshness_repository=KnowledgeIndexFreshnessRepository(conn),
            source_id=("wiki:" + ",".join(workspace_ids))[:128]
            if workspace_ids else "wiki:all-visible",
            auto_delete=os.environ.get("INDEX_AUTO_DELETE_ENABLE", "0") == "1",
            delete_max_count=int(os.environ.get("INDEX_DELETE_MAX_COUNT", "100")),
            delete_max_ratio=float(os.environ.get("INDEX_DELETE_MAX_RATIO", "0.1")),
        )
    except SourceSnapshotError as exc:
        conn.rollback()
        print(f"service=kb-sync-wiki status=degraded reason={exc}")
        raise SystemExit(1)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    reason = f" reason={stats['reason']}" if stats["reason"] else ""
    print(
        f"service=kb-sync-wiki status={stats['status']}{reason}"
        f" workspaces={stats['workspaces']} docs={stats['docs']}"
        f" chunks={stats['chunks']} failed={stats['failed']}"
        f" deleted={stats['deleted']} delete_planned={stats['delete_planned']}"
        f" index_version={stats['index_version']} output={out_path}"
    )
    if stats["status"] != "completed":
        raise SystemExit(1)
    return stats


if __name__ == "__main__":
    main()
