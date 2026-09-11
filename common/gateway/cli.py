# -*- coding: utf-8 -*-
"""dingtalk-gateway CLI（阶段 4 ``dingtalk-gateway`` 容器入口）。

轮询 ``robot_outbox`` 并投递到钉钉。这是**唯一**持钉钉凭据、**唯一**
外发消息的业务相关容器；region 配置来自种子 + Nacos 覆盖。

用法::

    # 长驻轮询（容器默认）
    python -m common.gateway.cli run --live-send --confirm-local-test-write \
        --source-credentials /run/live-input/source-credentials.json

    # 单轮投递（验收/调试）
    python -m common.gateway.cli run --once --live-send --confirm-local-test-write \
        --source-credentials /run/live-input/source-credentials.json

输出只有安全摘要行（service / delivered / failed 计数与状态码），绝不打
印凭据、消息载荷或异常原文。
"""

import argparse
import sys
import threading
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Lazy wrappers -- module-level so tests can patch them.
# ---------------------------------------------------------------------------

def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def require_gateway_run(settings, *, live_send, confirm_local_test_write):
    from common.public_data.live_safety import require_gateway_run as _require
    return _require(
        settings,
        live_send=live_send,
        confirm_local_test_write=confirm_local_test_write,
    )


def build_pipeline_config_source():
    from common.public_data.pipeline_config import build_config_source
    return build_config_source()


def resolve_service_id(override=None):
    from common.public_data.pipeline_config import resolve_service_id as _resolve
    return _resolve(override=override)


def load_region_configs(seed_path):
    from common.region_config import (
        apply_region_overlay,
        build_nacos_region_overlay,
        load_region_seed,
    )
    return apply_region_overlay(
        load_region_seed(seed_path), build_nacos_region_overlay()
    )


def load_source_credentials(path, source=None):
    from common.public_data.cli import load_source_credentials as _load
    return _load(path, source=source)


def connect_mart(settings):
    from common.public_data.db import connect
    return connect(settings.mart_database)


def build_deliverer(credentials, region_configs):
    """构造真实投递器：钉钉 client（凭据注入）+ region 路由表。"""
    from common.dingtalk import DingTalkClient
    from common.gateway import DingTalkDeliverer

    creds = credentials["dingtalk"]
    client = DingTalkClient(
        creds["app_key"], creds["app_secret"], creds["operator_id"]
    )
    region_config = {
        region: {
            "robot_code": cfg.robot_code,
            "open_conversation_id": cfg.open_conversation_id,
        }
        for region, cfg in region_configs.items()
    }
    return DingTalkDeliverer(client=client, region_config=region_config)


def build_worker(outbox, deliverer):
    from common.gateway import OutboxDeliveryWorker
    return OutboxDeliveryWorker(
        outbox=outbox,
        deliverer=deliverer,
        now=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
    )


def build_stream_handler(*, region_configs, connection_factory):
    from common.gateway import StreamReportHandler
    return StreamReportHandler(
        region_configs=region_configs,
        connection_factory=connection_factory,
    )


def build_stream_client(app_key, app_secret, handler):
    from common.gateway.stream_handler import build_stream_client as _build
    return _build(app_key, app_secret, handler)


def publish_regions(source_path, *, if_missing=False):
    from common.region_config import publish_regions_from_env
    return publish_regions_from_env(source_path, if_missing=if_missing)


def build_outbox(conn):
    from common.public_data.outbox_repository import OutboxRepository
    return OutboxRepository(conn)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _pipeline_enabled(service_id):
    if not service_id:
        return True
    try:
        return build_pipeline_config_source().get_pipeline(service_id).enabled
    except Exception:
        return True


def _handle_run(args):
    if not args.live_send or not args.confirm_local_test_write:
        sys.exit(1)
    if args.with_stream and not args.live_read:
        # Stream 是外部读取，须显式确认（与 outbox 的外发确认并列）。
        sys.exit(1)

    try:
        settings = load_settings()
        require_gateway_run(
            settings,
            live_send=args.live_send,
            confirm_local_test_write=args.confirm_local_test_write,
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            print("status=failed code=region_seed_required")
            sys.exit(1)
        region_configs = load_region_configs(seed_path)
        credentials = load_source_credentials(
            args.source_credentials, source="dingtalk"
        )

        deliverer = build_deliverer(credentials, region_configs)
        conn = connect_mart(settings)
        worker = build_worker(build_outbox(conn), deliverer)

        if args.with_stream:
            # 单进程双职责（spec §2）：outbox 轮询在守护线程，Stream 单连接
            # 在主线程 start_forever。
            handler = build_stream_handler(
                region_configs=region_configs,
                connection_factory=lambda: connect_mart(settings),
            )
            client = build_stream_client(
                credentials["dingtalk"]["app_key"],
                credentials["dingtalk"]["app_secret"],
                handler,
            )
            threading.Thread(
                target=worker.run_forever,
                kwargs={"interval_seconds": args.interval},
                daemon=True,
            ).start()
            print(
                f"service={service_id} status=listening "
                f"mode=outbox+stream regions={len(region_configs)}"
            )
            client.start_forever()
            return

        if args.once:
            report = worker.deliver_once()
            conn.commit()
            print(
                f"service={service_id} status=completed "
                f"delivered={report.delivered_count} "
                f"failed={report.failed_count}"
            )
            return

        worker.run_forever(interval_seconds=args.interval)
    except SystemExit:
        raise
    except Exception:
        print("status=failed code=gateway_error")
        sys.exit(1)


def _handle_publish_regions(args):
    try:
        count = publish_regions(args.source, if_missing=args.if_missing)
        print(f"published={count} regions")
    except SystemExit:
        raise
    except Exception:
        print("status=failed code=config_error")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dingtalk-gateway",
        description="Outbox delivery gateway (stage 4)",
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Poll robot_outbox and deliver")
    run.add_argument("--live-send", action="store_true", default=False)
    run.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    run.add_argument("--source-credentials", required=True)
    run.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    run.add_argument(
        "--once", action="store_true", default=False,
        help="deliver a single batch and exit (default: poll forever)",
    )
    run.add_argument(
        "--interval", type=int, default=30,
        help="poll interval in seconds for the long-running mode",
    )
    run.add_argument(
        "--with-stream", action="store_true", default=False,
        help="also run the Stream report listener in this process "
             "(requires --live-read)",
    )
    run.add_argument("--live-read", action="store_true", default=False)

    # -- publish-regions -------------------------------------------------------
    publish = subparsers.add_parser(
        "publish-regions",
        help="Publish real region configs to Nacos (group REGIONS)",
    )
    publish.add_argument("--source", required=True)
    publish.add_argument("--if-missing", action="store_true", default=False)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "publish-regions":
        _handle_publish_regions(args)
        return

    _handle_run(args)


if __name__ == "__main__":
    main()
