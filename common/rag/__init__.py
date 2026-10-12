# -*- coding: utf-8 -*-
"""数据中台 + 企业知识库 RAG：图文同空间向量检索 + LLM 答题。

定位（2026-10-12 裁决，第一期）：
- 数据源 = mart_ops.kb_products（824 条商品结构化字段 + 1314 张附件图片，
  图片占比 75%——第一期主打「文字搜图」跨模态场景，kb_qa 素材分支的 P2 升级）；
- embedding = jina-clip-v2（API 托管，多语言含中文；文本/图像同一向量空间，
  默认 1024 维支持 MRL 截断）；
- 向量库 = Milvus Lite 嵌入式（dops-ci 2C4G 跑不动 standalone，<1GB 内存）；
- 生成 = DeepSeek chat（base https://api.deepseek.com，OpenAI 格式）；
- 单 collection 图文混存，AUTOINDEX + COSINE；dim 建库后不可改，
  必须先用 scripts/clip_probe 实测 jina-clip-v2 真实输出维度再建生产库。

环境变量（凭据进 /opt/dops/app.env，不入库）：
    JINA_API_KEY            jina-clip-v2 API key（必填，embedding）
    JINA_CLIP_MODEL         默认 jina-clip-v2
    JINA_CLIP_DIM           生产向量维度（默认 1024，建库前以探针实测为准）
    RAG_MILVUS_PATH         Milvus Lite 数据文件路径（默认 /opt/vector/data/rag.db）
    DEEPSEEK_API_KEY        DeepSeek chat API key（仅 LLM 答题需要）
    DEEPSEEK_MODEL          默认 deepseek-chat
"""
