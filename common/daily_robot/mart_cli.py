# -*- coding: utf-8 -*-
"""日报机器人 mart 版 CLI（阶段 4 ``robot`` 容器入口）。

只读 ``mart_ops``（+ 写 ``robot_outbox``）：不碰 ``raw_*``、不碰源、
不挂钉钉凭据、不发任何外部请求。消息由 ``dingtalk-gateway`` 投递。

用法（cron 触发）::

    python -m common.daily_robot.mart_cli once --confirm-local-test-write
    python -m common.daily_robot.mart_cli remind --confirm-local-test-write
    python -m common.daily_robot.mart_cli check --confirm-local-test-write [--date 2026-09-11]

``once`` 按当前小时分流（region 配置的 ``remindHour`` / ``checkHour``），
与现行 cron 约定（``0 18,20 * * *``）一致。
"""

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Lazy wrappers -- module-level so tests can patch them.
# ---------------------------------------------------------------------------

def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def require_business_run(settings, *, confirm_local_test_write):
    from common.public_data.live_safety import require_business_run as _require
    return _require(settings, confirm_local_test_write=confirm_local_test_write)


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


def connect_mart(settings):
    from common.public_data.db import connect
    return connect(settings.mart_database)


def run_remind_task(conn, outbox, **kwargs):
    from common.daily_robot.mart_tasks import run_remind
    return run_remind(conn, outbox, **kwargs)


def run_check_task(conn, outbox, **kwargs):
    from common.daily_robot.mart_tasks import run_check
    return run_check(conn, outbox, **kwargs)


def build_outbox(conn):
    from common.public_data.outbox_repository import OutboxRepository
    return OutboxRepository(conn)


def mart_collect_data(conn, *, region, business_date):
    from common.daily_robot.mart_leaderboard import mart_collect
    return mart_collect(conn, region=region, business_date=business_date)


def build_view(region_cfg, workdays, *, year, month):
    from common.daily_robot.mart_leaderboard import build_leaderboard_view
    return build_leaderboard_view(region_cfg, workdays, year=year, month=month)


def render_bc(region, calendar, now, elapsed, people, url=None):
    from common.daily_robot.leaderboard import render_bc_markdown
    return render_bc_markdown(region, calendar, now, elapsed, people, url=url)


def build_html_page(view, now, elapsed, people):
    from common.daily_robot.leaderboard import build_html
    return build_html(view, now, elapsed, people)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pipeline_enabled(service_id):
    """注册表显式关停才跳过；任何注册表错误都 fail-open（不停摆）。"""
    if not service_id:
        return True
    try:
        return build_pipeline_config_source().get_pipeline(service_id).enabled
    except Exception:
        return True


def route_by_hour(hour, region_config):
    """``once`` 的分流：提醒小时 → remind，催办小时 → check，否则空。"""
    if hour == region_config.remind_hour:
        return ("remind",)
    if hour == region_config.check_hour:
        return ("check",)
    return ()


def _resolve_region(args):
    region = getattr(args, "region", None) or (
        os.environ.get("ROBOT_REGION") or ""
    ).strip()
    return region or None


def _print_failure(code):
    print(f"status=failed code={code}")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handle(args, kind=None):
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_business_run(
            settings, confirm_local_test_write=args.confirm_local_test_write
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        region = _resolve_region(args)
        if not region:
            _print_failure("region_required")
            sys.exit(1)

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            _print_failure("region_seed_required")
            sys.exit(1)
        configs = load_region_configs(seed_path)
        cfg = configs.get(region)
        if cfg is None:
            _print_failure("unknown_region")
            sys.exit(1)

        now = datetime.now()
        business_date = (
            date.fromisoformat(args.date) if getattr(args, "date", None)
            else now.date()
        )

        kinds = (kind,) if kind else route_by_hour(now.hour, cfg)
        if not kinds:
            print(
                f"service={service_id} region={region} "
                f"status=no_task hour={now.hour}"
            )
            return

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        for current_kind in kinds:
            if current_kind == "remind":
                outcome = run_remind_task(
                    conn, outbox,
                    region=region, display=cfg.display,
                    table_url=cfg.table_url,
                    business_date=business_date, now=now,
                    aliases=cfg.aliases,
                )
            else:
                outcome = run_check_task(
                    conn, outbox,
                    region=region, display=cfg.display,
                    table_url=cfg.table_url,
                    business_date=business_date, now=now,
                    cc_user_ids=cfg.cc_user_ids, aliases=cfg.aliases,
                )
            conn.commit()
            print(
                f"service={service_id} region={region} kind={current_kind} "
                f"status={outcome.status} unfilled={len(outcome.unfilled)}"
            )
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def _handle_leaderboard(args):
    """每日榜单播报：mart 采集 → 群播报 markdown → outbox（kind='leaderboard'）。"""
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_business_run(
            settings, confirm_local_test_write=args.confirm_local_test_write
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        region = _resolve_region(args)
        if not region:
            _print_failure("region_required")
            sys.exit(1)

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            _print_failure("region_seed_required")
            sys.exit(1)
        configs = load_region_configs(seed_path)
        cfg = configs.get(region)
        if cfg is None:
            _print_failure("unknown_region")
            sys.exit(1)

        now = datetime.now()
        business_date = (
            date.fromisoformat(args.date) if getattr(args, "date", None)
            else now.date()
        )

        conn = connect_mart(settings)
        data = mart_collect_data(conn, region=region, business_date=business_date)
        view = build_view(
            cfg, data.workdays,
            year=business_date.year, month=business_date.month,
        )
        body = render_bc(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people),
            url=cfg.leaderboard_url or None,
        )

        outbox = build_outbox(conn)
        enqueued = outbox.enqueue(
            region=region,
            kind="leaderboard",
            business_date=business_date,
            title="销售完成率榜",
            body_md=body,
            created_at=now,
            dedupe_suffix=f"{now:%H%M}",
        )
        conn.commit()
        print(
            f"service={service_id} region={region} kind=leaderboard "
            f"status={'enqueued' if enqueued else 'already_sent'} "
            f"people={len(data.people)}"
        )
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def fetch_channel_month_facts(connection, *, year, month):
    from common.daily_robot.channel_daily import (
        fetch_channel_month_facts as _fetch,
    )
    return _fetch(connection, year=year, month=month)


def build_channel_section(**kwargs):
    from common.daily_robot.channel_daily import build_channel_section as _build
    return _build(**kwargs)


def _handle_channel_daily(args):
    """电商渠道日报（qudao 三段式）：渠道日销快报 + 人员完成率榜 → outbox。

    outbox kind='channel_daily'（与 leaderboard 独立去重键）；无渠道数据
    的日期只发人员榜部分（区域无渠道线时不报错）。
    """
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_business_run(
            settings, confirm_local_test_write=args.confirm_local_test_write
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        region = _resolve_region(args)
        if not region:
            _print_failure("region_required")
            sys.exit(1)

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            _print_failure("region_seed_required")
            sys.exit(1)
        configs = load_region_configs(seed_path)
        cfg = configs.get(region)
        if cfg is None:
            _print_failure("unknown_region")
            sys.exit(1)

        now = datetime.now()
        business_date = (
            date.fromisoformat(args.date) if getattr(args, "date", None)
            else now.date()
        )

        conn = connect_mart(settings)
        data = mart_collect_data(conn, region=region, business_date=business_date)
        view = build_view(
            cfg, data.workdays,
            year=business_date.year, month=business_date.month,
        )
        people_body = render_bc(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people),
            url=cfg.leaderboard_url or None,
        )

        # 渠道日销段：业务日 = 昨日（含）之前最近一个有渠道数据的工作日
        # （店铺后台导出存在时滞，预填空行日自动回退，不报 0 元假数据）。
        section = ""
        month_facts = fetch_channel_month_facts(
            conn, year=business_date.year, month=business_date.month
        )
        # 按日合计非零判定有效数据日（预填 0 值行不算，防止"0 元假日报"）
        day_totals = {}
        for facts in month_facts.values():
            for d, amount in facts.items():
                if d < business_date:
                    day_totals[d] = day_totals.get(d, 0) + amount
        filled_days = sorted(d for d, total in day_totals.items() if total != 0)
        if filled_days:
            channel_date = filled_days[-1]
            section = build_channel_section(
                month_facts=month_facts,
                business_date=channel_date,
                workdays=data.workdays,
                monthly_targets=cfg.monthly_targets,
            )
        body = f"{section}\n\n{people_body}" if section else people_body

        outbox = build_outbox(conn)
        enqueued = outbox.enqueue(
            region=region,
            kind="channel_daily",
            business_date=business_date,
            title="电商渠道日报",
            body_md=body,
            created_at=now,
            dedupe_suffix=f"{now:%H%M}",
        )
        conn.commit()
        print(
            f"service={service_id} region={region} kind=channel_daily "
            f"status={'enqueued' if enqueued else 'already_sent'} "
            f"section={'channel' if section else 'people_only'} "
            f"people={len(data.people)}"
        )
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def _handle_leaderboard_html(args):
    """榜单页面：mart 采集 → 既有 HTML 构建 → 写文件（发布通道维持现状）。"""
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_business_run(
            settings, confirm_local_test_write=args.confirm_local_test_write
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        region = _resolve_region(args)
        if not region:
            _print_failure("region_required")
            sys.exit(1)

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            _print_failure("region_seed_required")
            sys.exit(1)
        configs = load_region_configs(seed_path)
        cfg = configs.get(region)
        if cfg is None:
            _print_failure("unknown_region")
            sys.exit(1)

        now = datetime.now()
        business_date = (
            date.fromisoformat(args.date) if getattr(args, "date", None)
            else now.date()
        )

        conn = connect_mart(settings)
        data = mart_collect_data(conn, region=region, business_date=business_date)
        view = build_view(
            cfg, data.workdays,
            year=business_date.year, month=business_date.month,
        )
        page = build_html_page(view, now, list(data.elapsed), list(data.people))

        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(page, encoding="utf-8")
        print(
            f"service={service_id} region={region} kind=leaderboard-html "
            f"status=written people={len(data.people)}"
        )
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="daily-robot",
        description="Daily-report robot on mart_ops (stage 4)",
    )
    subparsers = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("remind", "Enqueue the 18:30 reminder into robot_outbox"),
        ("check", "Enqueue the 20:00 check + DING into robot_outbox"),
        ("once", "Route to remind/check by the current hour"),
        ("leaderboard", "Enqueue the daily leaderboard broadcast"),
        ("channel-daily", "Enqueue the ecom channel daily report (qudao composite)"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--confirm-local-test-write", action="store_true", default=False
        )
        sub.add_argument(
            "--region", default=None,
            help="business region (default: $ROBOT_REGION)",
        )
        sub.add_argument(
            "--service", default=None,
            help="pipeline service id for the registry enable gate "
                 "(default: $PUBLIC_DATA_SERVICE_ID)",
        )
        sub.add_argument(
            "--date", default=None,
            help="business date override (YYYY-MM-DD, default: today)",
        )

    # -- leaderboard-html ------------------------------------------------------
    html_sub = subparsers.add_parser(
        "leaderboard-html", help="Render the leaderboard HTML page to a file"
    )
    html_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    html_sub.add_argument(
        "--region", default=None,
        help="business region (default: $ROBOT_REGION)",
    )
    html_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    html_sub.add_argument(
        "--date", default=None,
        help="business date override (YYYY-MM-DD, default: today)",
    )
    html_sub.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "leaderboard":
        _handle_leaderboard(args)
        return
    if args.command == "channel-daily":
        _handle_channel_daily(args)
        return
    if args.command == "leaderboard-html":
        _handle_leaderboard_html(args)
        return

    _handle(args, kind=None if args.command == "once" else args.command)


if __name__ == "__main__":
    main()
