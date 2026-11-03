"""scheduler 子命令与 PRICE_AGENT 非敏感配置发布工具。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common.price_agent.config import (
    DATA_ID,
    DEFAULT_SEED,
    GROUP,
    build_config_source,
    parse_config,
)
from common.price_agent.service import dispatch_job, expire_jobs
from common.public_data.db import connect
from common.public_data.nacos_client import build_nacos_client
from common.public_data.settings import Settings


def _parser():
    parser = argparse.ArgumentParser(prog="price-agent")
    parser.add_argument("action", choices=("dispatch", "finalize", "publish-config"))
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    parser.add_argument("--run-history-id", type=int)
    parser.add_argument("--trigger-source", choices=("cron", "run_once"), default="cron")
    parser.add_argument("--if-missing", action="store_true")
    return parser


def _publish_config(if_missing):
    env = os.environ
    seed = Path(env.get("PUBLIC_DATA_PRICE_AGENT_SEED") or DEFAULT_SEED)
    text = seed.read_text(encoding="utf-8")
    _, digest = parse_config(text)
    client = build_nacos_client(
        (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip(),
        namespace=(env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip(),
        username=(env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None,
        password=(env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None,
    )
    if if_missing and client.get_config(DATA_ID, GROUP):
        return {"status": "skipped_existing", "data_id": DATA_ID}
    client.publish_config(DATA_ID, GROUP, text)
    return {"status": "published", "data_id": DATA_ID, "config_hash": digest}


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.live_read or not args.confirm_local_test_write:
        print("refusing to run without --live-read and --confirm-local-test-write")
        return 2
    if args.action == "publish-config":
        result = _publish_config(args.if_missing)
    else:
        settings = Settings.from_environment()
        with connect(settings.mart_database) as connection:
            if args.action == "dispatch":
                config, digest = build_config_source().get_config()
                result = dispatch_job(
                    connection,
                    config,
                    digest,
                    source=args.trigger_source,
                    run_history_id=args.run_history_id,
                )
            else:
                result = expire_jobs(connection)
    print("service=price-maochao-" + args.action + " " + json.dumps(
        result, ensure_ascii=False, sort_keys=True, default=str
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
