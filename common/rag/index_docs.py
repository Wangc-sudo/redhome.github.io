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

幂等：条目 id = ``doc:{doc_id}:{chunk_index:04d}``；导入前按 ``doc:{doc_id}:``
前缀清空旧切片——文档改版后切片数变少也不留残块，重跑零脏数据（数据层幂等，
手动触发无需 --force）。

平台标准形态：
- 调度：scheduler 命令表 ``rag-index-docs``；
- 门禁：``require_business_run``（只读本地清单 + 写本地向量库，无外部调用）；
- fail-soft：缺 JINA_API_KEY 或清单文件不存在 → 打印 skipped 静默跳过
  （灰度期 key 未到位的常规态，不刷失败告警）。

环境变量（凭据进 /opt/dops/app.env，不入库）：
    JINA_API_KEY / JINA_CLIP_MODEL / JINA_CLIP_DIM / RAG_MILVUS_PATH
    RAG_DOCS_INPUT   文档清单路径（默认 /opt/dops/config-local/kb_docs.json）

用法::

    python3 -m common.rag.index_docs --file /tmp/kb_docs.json --confirm-local-test-write

清单 JSON 形如::

    [{"doc_id": "amweZ92PV6wQbnYGHq5KD5mqJxEKBD6p",
      "doc_title": "京东Q3销售总结及Q4规划", "content": "# 摘要\\n..."}]
"""

import argparse
import html
import json
import os
import re
from pathlib import Path

from common.rag import embed

#: 清单缺省路径：与其余本地输入同目录（config-local 只读挂载）。
DEFAULT_INPUT = "/opt/dops/config-local/kb_docs.json"

#: 切片窗口与重叠（字）。600 字≈一段报告正文，80 字重叠防句义被腰斩。
CHUNK_SIZE = 600
CHUNK_OVERLAP = 80

#: 嵌入批大小（jina 单请求多条；过大易触发限流，过小浪费往返）。
TEXT_BATCH = 16

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
    return _BLANK_RE.sub("\n\n", out).strip()


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


def _chunk_id(doc_id, index):
    return f"doc:{doc_id}:{index:04d}"


def run_index(payload, *, milvus, store, jina_key, dim, model=embed.DEFAULT_MODEL,
              size=CHUNK_SIZE, overlap=CHUNK_OVERLAP, text_batch=TEXT_BATCH,
              on_progress=None):
    """文档清单 → 向量库。返回统计 dict。

    逐文档处理：先清前缀（幂等底线）→ 批量 embed → upsert。单篇失败直接
    上抛，不留半写状态（宁可整批失败由调度重试，也不让库里出现残缺文档）。
    """
    real_dim = store.ensure_collection(milvus, dim)
    stats = {"docs": 0, "chunks": 0, "skipped": 0, "written": 0}
    for raw in payload:
        normalized = normalize_doc(raw)
        if normalized is None:
            stats["skipped"] += 1
            continue
        doc_id, title, content = normalized
        texts = build_chunks(content, size=size, overlap=overlap)
        if not texts:
            stats["skipped"] += 1
            continue
        store.delete_prefix(milvus, f"doc:{doc_id}:")
        vectors = []
        for start in range(0, len(texts), text_batch):
            part = texts[start:start + text_batch]
            vectors.extend(
                embed.embed_items([{"text": t} for t in part], jina_key,
                                  model=model, dimensions=real_dim)
            )
        items = [
            {
                "id": _chunk_id(doc_id, index),
                "vector": vector,
                "kind": "text",
                "name": title,
                "text": text,
                "image_ref": "",
            }
            for index, (text, vector) in enumerate(zip(texts, vectors))
        ]
        stats["written"] += store.upsert_items(milvus, items)
        stats["docs"] += 1
        stats["chunks"] += len(items)
        if on_progress:
            on_progress(doc_id, stats["chunks"])
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rag.index_docs")
    parser.add_argument("--file", help=f"文档清单 JSON（默认 {DEFAULT_INPUT}）")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    # fail-soft：key 未到位时每小时静默跳过，不刷失败告警；key 到位即生效。
    jina_key = os.environ.get("JINA_API_KEY")
    if not jina_key:
        print("service=rag-index-docs status=skipped reason=no_jina_key")
        return

    path = Path(args.file or os.environ.get("RAG_DOCS_INPUT") or DEFAULT_INPUT)
    if not path.is_file():
        print(f"service=rag-index-docs status=skipped reason=no_input"
              f" file={path}")
        return

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
    dim = int(os.environ.get("JINA_CLIP_DIM", str(embed.DEFAULT_DIM)))
    model = os.environ.get("JINA_CLIP_MODEL", embed.DEFAULT_MODEL)

    stats = run_index(payload, milvus=milvus, store=store, jina_key=jina_key,
                      dim=dim, model=model)
    print(
        f"service=rag-index-docs status=completed"
        f" docs={stats['docs']} chunks={stats['chunks']}"
        f" skipped={stats['skipped']} written={stats['written']}"
    )


if __name__ == "__main__":
    main()
