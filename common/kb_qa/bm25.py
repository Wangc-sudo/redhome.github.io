# -*- coding: utf-8 -*-
"""纯 Python BM25（字符二元组分词，中文检索零依赖）。

选型背景（2026-10-10）：天翼 token 套餐未含 embedding 模型，文档切片
量级仅几百——BM25 关键词检索足够，后续升级 embedding 只换检索层。
中文不依赖 jieba：字符 bigram 对中文检索效果稳定（「酱香酒」→
酱香/香酒），英文/数字按连续串成词。
"""

import math
import re
from collections import Counter

_ALNUM_RUN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """中文取字符 bigram（单字保留），英文/数字取连续小写串。"""
    tokens = []
    han_run = []
    alnum_run = []

    def flush_alnum():
        if alnum_run:
            tokens.extend(_ALNUM_RUN_RE.findall("".join(alnum_run)))
            alnum_run.clear()

    def flush_han():
        if han_run:
            tokens.extend(_flush_han(han_run))
            han_run.clear()

    for char in (text or "").lower():
        if "一" <= char <= "鿿":
            flush_alnum()
            han_run.append(char)
        elif char.isascii() and char.isalnum():
            flush_han()
            alnum_run.append(char)
        else:
            flush_alnum()
            flush_han()
    flush_alnum()
    flush_han()
    return tokens


def _flush_han(run):
    if not run:
        return []
    if len(run) == 1:
        return [run[0]]
    bigrams = [run[i] + run[i + 1] for i in range(len(run) - 1)]
    return bigrams + list(run)


class Bm25Index:
    """k1=1.5, b=0.75 标准 BM25。文档量几百，全量内存评分即可。"""

    def __init__(self, documents, *, k1=1.5, b=0.75):
        self._k1 = k1
        self._b = b
        self._doc_tokens = [tokenize(doc) for doc in documents]
        self._doc_len = [len(tokens) for tokens in self._doc_tokens]
        self._avg_len = (
            sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0
        )
        self._doc_freq = Counter()
        self._term_freq = []
        for tokens in self._doc_tokens:
            counts = Counter(tokens)
            self._term_freq.append(counts)
            for term in counts:
                self._doc_freq[term] += 1
        self._n_docs = len(documents)

    def topk(self, query, k=3):
        """返回 [(doc_index, score)] 按分数降序，零分文档不返回。"""
        scores = []
        for index, counts in enumerate(self._term_freq):
            score = 0.0
            for term in set(tokenize(query)):
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                df = self._doc_freq[term]
                idf = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
                length = self._doc_len[index] or 1
                norm = 1 - self._b + self._b * length / (self._avg_len or 1)
                score += idf * tf * (self._k1 + 1) / (tf + self._k1 * norm)
            if score > 0:
                scores.append((index, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores[:k]
