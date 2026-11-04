#!/usr/bin/env bash
# 线上渠道独立质量门：复用隔离 MySQL/test-runner，只运行发布清单中的测试。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose -f docker-compose.integration.yml)
trap '"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true' EXIT

mapfile -t TESTS < <(python3 -c 'import json; print("\n".join(json.load(open("ops/channel-release.json", encoding="utf-8"))["tests"]))')
if [ "${#TESTS[@]}" -eq 0 ]; then
  echo "channel release manifest has no tests" >&2
  exit 1
fi

"${COMPOSE[@]}" run --rm --build --entrypoint python3 test-runner \
  -m unittest -v "${TESTS[@]}"
