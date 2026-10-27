# -*- coding: utf-8 -*-
"""小易意图路由：规则表 + 置信度 + 多意图识别。

规则命中置信度为 0.8，单号/纯数字等正则命中为 1.0，疑似商品与
未命中兜底为 0.3。新增意图只需增加一条 IntentRule，不改控制流。
"""

import re
from dataclasses import dataclass

_STRIP_CHARS_RE = re.compile(r"[「」【】《》〈〉\"'“”‘’()（）\[\]{}]")

PRODUCT_PARAM = "PRODUCT_PARAM"
PRODUCT_FILTER = "PRODUCT_FILTER"
PRODUCT_ASSET = "PRODUCT_ASSET"
PHONE_LOOKUP = "PHONE_LOOKUP"
ORDER_QUERY = "ORDER_QUERY"
SALES_QUERY = "SALES_QUERY"
DOC_QA = "DOC_QA"
CHITCHAT = "CHITCHAT"

_ASSET_WORDS = (
    "细节图", "质检", "素材", "礼袋图", "照片", "图片", "发票",
    "报告", "附件", "文件", "视频", "发我", "来张", "发张",
)
_PARAM_WORDS = (
    "箱规", "盒规", "尺寸", "重量", "材质", "69码", "条码",
    "规格", "编码", "礼袋", "多重", "多大", "几瓶装", "几件",
)
_ORDER_WORDS = (
    "快递到哪了", "物流查询", "什么时候到", "哪天发的", "这笔单子",
    "发到哪了", "签收了吗", "哪个客户", "谁买的", "到哪了",
    "订单", "单号", "交易", "发货", "物流", "快递", "运单",
    "顺丰", "德邦", "退货", "退款", "这单",
)
_SALES_WORDS = (
    "销售额", "销售金额", "销量", "卖了多少", "卖了", "出货", "出了多少",
    "完成率", "达成率", "达成", "营业额", "流水", "排名", "排行",
    "环比", "同比", "店铺", "门店",
)
_PHONE_WORDS = (
    "手机号码", "是谁的号", "这个号谁的", "手机是谁的", "验证码找谁",
    "哪台手机", "登录账号", "平台账号", "店铺账号", "谁在用",
    "尾号", "验证码", "手机号", "电话号", "号码",
)
_FILTER_WORDS = ("有哪些", "哪些", "所有", "全部", "列表", "几款")
_DOC_WORDS = (
    "怎么介绍", "介绍", "卖点", "工艺", "话术", "品牌故事", "三同两不同",
    "是什么", "怎么说",
)

_CHITCHAT_EXACT = {
    "你好", "您好", "在吗", "在不在", "早上好", "晚上好", "嗨", "哈喽",
    "hi", "hello", "谢谢", "感谢", "好的", "好", "嗯", "哦", "ok",
}

_NOISE_WORDS = tuple(sorted(set(
    (
        "请问", "那个", "哪个", "帮我", "看下", "看看", "我想", "我要",
        "有没有", "告诉", "麻烦", "一下", "多少", "什么", "怎么", "怎么样",
        "吗", "呢", "吧", "的", "了", "有", "是", "？", "?", "！", "!",
        "，", ",", "。", ".",
    ) + _ASSET_WORDS + _PARAM_WORDS + _FILTER_WORDS + _DOC_WORDS
), key=len, reverse=True))

# 旧问句「怎么跟客户介绍」需要去掉句中疑问词；其余噪声一律只按边界或
# 句首句尾剥离，避免全局 replace 误伤品名（如品名中间含「看」）。
_INNER_QUESTION_WORDS = ("怎么样", "怎么", "什么")


@dataclass(frozen=True)
class Intent:
    category: str
    keyword: str
    reason: str
    confidence: float = 0.3


@dataclass(frozen=True)
class IntentRule:
    category: str
    words: tuple
    patterns: tuple
    priority: int
    noise_strip: bool


_RULES = tuple(sorted((
    IntentRule(PHONE_LOOKUP, _PHONE_WORDS, (r"^\d{4,11}$",), 10, True),
    IntentRule(ORDER_QUERY, _ORDER_WORDS, (r"[A-Z]{2}\d{10,}",), 20, True),
    IntentRule(PRODUCT_ASSET, _ASSET_WORDS, (), 30, True),
    IntentRule(PRODUCT_PARAM, _PARAM_WORDS, (), 40, True),
    IntentRule(SALES_QUERY, _SALES_WORDS, (), 50, True),
    IntentRule(PRODUCT_FILTER, _FILTER_WORDS, (), 60, True),
    IntentRule(DOC_QA, _DOC_WORDS, (), 70, True),
), key=lambda rule: rule.priority))


def _bounded_noise(word):
    return re.compile(
        r"(?<![0-9A-Za-z\u4e00-\u9fff])" + re.escape(word)
        + r"(?![0-9A-Za-z\u4e00-\u9fff])"
    )


def extract_keyword(text):
    """剥离问句噪声与触发词，保留品名中间的噪声子串。"""
    keyword = _STRIP_CHARS_RE.sub(" ", (text or "").strip())
    for word in _INNER_QUESTION_WORDS:
        keyword = keyword.replace(word, " ")
    # 每轮最多从两端各剥一个词；循环覆盖「请问/帮我/看下」等连续前缀及
    # 「的重量是多少呢？」等连续后缀。中文之间的单字噪声不做全局替换。
    for _ in range(24):
        before = keyword
        keyword = keyword.strip()
        for word in _NOISE_WORDS:
            if keyword.startswith(word):
                keyword = keyword[len(word):].lstrip()
                break
        for word in _NOISE_WORDS:
            if keyword.endswith(word):
                keyword = keyword[:-len(word)].rstrip()
                break
        for word in _NOISE_WORDS:
            keyword = _bounded_noise(word).sub(" ", keyword)
        keyword = " ".join(keyword.split())
        if keyword == before:
            break
    return " ".join(keyword.split())


def classify_all(text):
    """返回全部命中意图，按 IntentRule.priority 升序排列。"""
    normalized = (text or "").strip()
    if not normalized:
        return [Intent(CHITCHAT, "", "空消息")]
    if normalized.lower() in _CHITCHAT_EXACT:
        return [Intent(CHITCHAT, normalized, "闲聊/问候", 1.0)]

    keyword = extract_keyword(normalized)
    intents = []
    for rule in _RULES:
        pattern = next(
            (p for p in rule.patterns if re.search(p, normalized, re.IGNORECASE)),
            None,
        )
        if pattern:
            intents.append(Intent(
                rule.category, keyword if rule.noise_strip else normalized,
                f"命中{rule.category}正则", 1.0,
            ))
            continue
        word = next((w for w in rule.words if w in normalized), None)
        if word:
            intents.append(Intent(
                rule.category, keyword if rule.noise_strip else normalized,
                f"命中{rule.category}词「{word}」", 0.8,
            ))
    if intents:
        return intents
    if keyword and len(keyword) >= 2:
        return [Intent(PRODUCT_PARAM, keyword, "疑似商品名直查", 0.3)]
    return [Intent(CHITCHAT, keyword, "未命中任何规则", 0.3)]


def classify(text):
    """单意图兼容入口：取优先级最高的命中意图。"""
    return classify_all(text)[0]
