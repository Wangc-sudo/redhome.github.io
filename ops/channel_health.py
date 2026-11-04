"""线上渠道发布健康门：只读校验任务所有权、命令映射和调度心跳。"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.public_data.scheduler import command_spec, load_service_ids


def main():
    manifest = json.loads((ROOT / "ops" / "channel-release.json").read_text(encoding="utf-8"))
    expected = tuple(manifest["scheduler_services"])
    actual = load_service_ids()
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise RuntimeError(
            f"渠道调度所有权不一致：expected={sorted(expected)} actual={sorted(actual)}"
        )
    missing_commands = [service_id for service_id in expected if command_spec(service_id) is None]
    if missing_commands:
        raise RuntimeError(f"渠道任务缺少命令映射：{missing_commands}")
    heartbeat = Path(os.environ["SCHEDULER_HEARTBEAT_PATH"])
    age = time.time() - heartbeat.stat().st_mtime
    if age >= 120:
        raise RuntimeError(f"渠道调度心跳过旧：{age:.1f}s")
    print(json.dumps({
        "status": "ok",
        "release": manifest["release"],
        "pipelines": len(actual),
        "heartbeat_age_seconds": round(age, 1),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
