# -*- coding: utf-8 -*-
"""评测集回放（2026-10-29 方案 §7/D6，2026-11-02 落地）。

逐句跑真实 run_agent（真实 LLM + 真实库，只读），断言：
- expected_tools ⊆ 实际调用工具（空数组=不断言工具路由，LLM 有方差）；
- must_contain 全部出现；must_not_contain 全部不出现。

用法（云上 kb-gateway 容器，只读无写入）：
  docker exec -i -e PYTHONPATH=/opt/dops/repo -w /opt/dops/repo kb-gateway \
    python3 -m tests.kb_qa_eval.replay --limit 20 --output /tmp/eval.json

抽样建议 --limit/--only 控制成本；全量 100 句约数十分钟，供夜间或发版前跑。
"""

import argparse
import json
import time
from pathlib import Path

from common.kb_qa import llm as llm_mod
from common.kb_qa import rag_docs
from common.kb_qa.agent import ToolExecutor, run_agent
from common.kb_qa.docs import search_docs
from common.kb_qa.focus import resolve_boundary_reply
from common.public_data.db import connect
from common.public_data.settings import Settings

CASES = Path(__file__).with_name("cases.jsonl")


def load_cases(path=None):
    source = Path(path) if path else CASES
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def run_case(case, *, llm, conn, vector_search):
    started = time.monotonic()
    # 与 app._try_agent 入口一致：同比基期早于数据起点时直答不跑工具，
    # 否则回放会把边界句误判成未直答（首轮实测 FAIL 即此缺口）。
    boundary = resolve_boundary_reply(conn, case["question"])
    tools = []
    if boundary:
        answer = boundary
    else:
        executor = ToolExecutor(conn, doc_search=search_docs,
                                vector_search=vector_search)
        meta = {}
        try:
            answer = run_agent(llm, case["question"], executor=executor,
                               meta=meta) or ""
        except Exception as exc:  # 单句失败不中断回放，记为失败样本
            return {
                "question": case["question"], "pass": False,
                "failures": [f"exception: {type(exc).__name__}"],
                "tools": meta.get("tools") or [], "latency_ms": 0,
                "answer": "",
            }
        tools = meta.get("tools") or []
    failures = []
    if case["expected_tools"]:
        missing = [t for t in case["expected_tools"] if t not in tools]
        if missing:
            failures.append(f"expected_tools 未调用: {missing}")
    for token in case["must_contain"]:
        if token not in answer:
            failures.append(f"must_contain 缺失: {token!r}")
    for token in case["must_not_contain"]:
        if token in answer:
            failures.append(f"must_not_contain 命中: {token!r}")
    return {
        "question": case["question"], "pass": not failures,
        "failures": failures, "tools": tools,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "answer": answer,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start", type=int, default=0,
                        help="跳过前 N 句（断点续跑，容器重启后从中断处继续）")
    parser.add_argument("--only", default=None, help="问题关键词过滤")
    parser.add_argument("--output", default=None, help="完整结果 JSON 输出路径")
    args = parser.parse_args()

    cases = list(load_cases(args.cases))
    if args.only:
        cases = [c for c in cases if args.only in c["question"]]
    if args.start:
        cases = cases[args.start:]
    if args.limit:
        cases = cases[:args.limit]

    settings = Settings.from_environment()
    llm = llm_mod.from_environment()
    conn = connect(settings.mart_database)
    vector_search = rag_docs.from_environment()

    results = []
    for index, case in enumerate(cases, 1):
        result = run_case(case, llm=llm, conn=conn,
                          vector_search=vector_search)
        results.append(result)
        mark = "PASS" if result["pass"] else "FAIL"
        print(f"[{index}/{len(cases)}] {mark} {case['question'][:40]}"
              f" tools={result['tools']} {result['latency_ms']}ms")
        for failure in result["failures"]:
            print(f"    - {failure}")

    conn.close()
    passed = sum(1 for r in results if r["pass"])
    print(f"\n== {passed}/{len(results)} 通过 ==")
    if args.output:
        Path(args.output).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整结果已写入 {args.output}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
