# -*- coding: utf-8 -*-
"""意图路由（P1 纯规则；客服场景问题高度集中，规则先覆盖约 80%）。

分类优先级：PRODUCT_ASSET > PRODUCT_PARAM > PRODUCT_FILTER > DOC_QA > CHITCHAT。
P2 接入 LLM 时本模块输出作为规则层，未命中再走 qwen/DeepSeek 分类兜底。

2026-10-10 运维裁决「全数据非敏感」：无 BLOCKED 类别，无角色门禁。
"""

from dataclasses import dataclass

PRODUCT_PARAM = "PRODUCT_PARAM"
PRODUCT_FILTER = "PRODUCT_FILTER"
PRODUCT_ASSET = "PRODUCT_ASSET"
DOC_QA = "DOC_QA"
CHITCHAT = "CHITCHAT"

#: 索取图片/文件 → 返回资源分支。
_ASSET_WORDS = (
    "细节图", "质检", "素材", "礼袋图", "照片", "图片", "发票",
    "报告", "附件", "文件", "视频", "发我", "来张", "发张",
)
#: 商品具体参数查询。
_PARAM_WORDS = (
    "箱规", "盒规", "尺寸", "重量", "材质", "69码", "条码",
    "规格", "编码", "礼袋", "多重", "多大", "几瓶装", "几件",
)
#: 按条件筛选商品列表。
_FILTER_WORDS = ("有哪些", "哪些", "所有", "全部", "列表", "几款")
#: 手册类（卖点/工艺/话术）——P2 接 RAG，P1 给兜底文案。
_DOC_WORDS = (
    "怎么介绍", "介绍", "卖点", "工艺", "话术", "品牌故事", "三同两不同",
    "是什么", "怎么说",
)

#: 闲聊/问候精确匹配（短词会被「疑似商品名直查」兜底误吞，须先拦）。
_CHITCHAT_EXACT = {
    "你好", "您好", "在吗", "在不在", "早上好", "晚上好", "嗨", "哈喽",
    "hi", "hello", "谢谢", "感谢", "好的", "好", "嗯", "哦", "ok",
}

#: 提取商品关键词时剥离的问句噪音（长词在前，先剥长的）。
_NOISE_WORDS = (
    "请问", "一下", "多少", "什么", "怎么", "怎么样", "吗", "呢", "吧",
    "的", "了", "有", "是", "？", "?", "！", "!", "，", ",", "。", ".",
) + _ASSET_WORDS + _PARAM_WORDS + _FILTER_WORDS + _DOC_WORDS


@dataclass(frozen=True)
class Intent:
    category: str
    keyword: str
    reason: str


def extract_keyword(text):
    """剥离触发词与问句噪音，剩下的作为商品名/检索关键词。"""
    keyword = text.strip()
    for word in _NOISE_WORDS:
        keyword = keyword.replace(word, " ")
    return " ".join(keyword.split())


def classify(text):
    """纯规则意图分类。空文本归 CHITCHAT。"""
    normalized = (text or "").strip()
    if not normalized:
        return Intent(CHITCHAT, "", "空消息")
    if normalized.lower() in _CHITCHAT_EXACT:
        return Intent(CHITCHAT, normalized, "闲聊/问候")
    keyword = extract_keyword(normalized)
    for word in _ASSET_WORDS:
        if word in normalized:
            return Intent(PRODUCT_ASSET, keyword, f"命中附件词「{word}」")
    for word in _PARAM_WORDS:
        if word in normalized:
            return Intent(PRODUCT_PARAM, keyword, f"命中参数词「{word}」")
    for word in _FILTER_WORDS:
        if word in normalized:
            return Intent(PRODUCT_FILTER, keyword, f"命中筛选词「{word}」")
    for word in _DOC_WORDS:
        if word in normalized:
            return Intent(DOC_QA, keyword, f"命中手册词「{word}」")
    # 无触发词但像商品名（>=2 个有效字符）→ 按参数查询处理，查不到再兜底。
    if keyword and len(keyword) >= 2:
        return Intent(PRODUCT_PARAM, keyword, "疑似商品名直查")
    return Intent(CHITCHAT, keyword, "未命中任何规则")
