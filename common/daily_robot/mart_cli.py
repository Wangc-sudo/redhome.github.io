# -*- coding: utf-8 -*-
"""线上渠道 mart CLI（渠道日报/到齐检查/日历看门/新鲜度看门入口）。

只读 ``mart_ops``（+ 写 ``robot_outbox``）：不碰 ``raw_*``、不碰源、
不挂钉钉凭据、不发任何外部请求。消息由 ``dingtalk-gateway`` 投递。

用法（cron 触发）::

    python -m common.daily_robot.mart_cli channel-daily --confirm-local-test-write
    python -m common.daily_robot.mart_cli channel-missing --confirm-local-test-write
    python -m common.daily_robot.mart_cli calendar-watch --confirm-local-test-write
    python -m common.daily_robot.mart_cli freshness-check --confirm-local-test-write

线下业务线的提醒/催办/榜单/目标结转命令（``once`` / ``remind`` / ``check`` /
``leaderboard`` / ``leaderboard-html`` / ``offline-*`` / ``target-*``）已于
2026-11-18 随线下仓剥离迁到 offline-branch（``common.daily_robot.offline_cli``）。
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
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


def load_region_configs(seed_path, connection=None):
    """读序：seed 文件 → Nacos 覆盖 → DB 覆盖（dim_org_region）。

    2026-10-26 权威源迁 DB：页面编辑即生效。connection=None 时自建短
    连接读一层；DB 不可达 fail-open 保留 seed/Nacos 口径（不阻断播报）。
    """
    from common.region_config import (
        apply_db_overlay,
        apply_region_overlay,
        build_nacos_region_overlay,
        load_region_seed,
    )
    configs = apply_region_overlay(
        load_region_seed(seed_path), build_nacos_region_overlay()
    )
    own = False
    if connection is None:
        try:
            from common.public_data.settings import Settings
            settings = Settings.from_environment()
            connection = connect_mart(settings)
            own = True
        except Exception:
            connection = None
    if connection is not None:
        try:
            configs = apply_db_overlay(configs, connection)
        finally:
            if own:
                connection.close()
    return configs


def connect_mart(settings):
    from common.public_data.db import connect
    return connect(settings.mart_database)


def build_outbox(conn):
    from common.public_data.outbox_repository import OutboxRepository
    return OutboxRepository(conn)


def run_calendar_watch_task(conn, outbox, **kwargs):
    from common.daily_robot.calendar_watch import run_watch
    return run_watch(conn, outbox, **kwargs)


def channel_dept_overrides(conn, region, data):
    """qudao 榜单的店铺粒度部门真值覆盖（其余区域返回 ``None`` = 原口径）。

    修复（2026-09-23 核查）：qudao 人员事实是「整店归集合每人」的个人
    考核口径，部门榜按 Σ(每人) 聚合时共管店重复计数（直播 4.4x、
    猫超 2x、私域 2x）；override 改用店铺粒度链（渠道日销 + 店铺月
    目标，与 AI 表真值一致）。表未迁移时函数内部 fail-open 返回 {}。
    """
    if region != "qudao":
        return None
    from common.metrics.daily_report import fetch_channel_dept_rollup
    bd = data.business_date
    # 自然日累计（月内 ≤ 当天；当天预填 0 行不影响）——对齐 AI 表仪表盘口径
    return fetch_channel_dept_rollup(
        conn, year=bd.year, month=bd.month, through=bd
    )


def channel_monthly_targets(conn, region, cfg):
    """渠道日销快报的月目标：店铺粒度真值优先，Nacos ``monthlyTargets`` 兜底。"""
    if region != "qudao":
        return cfg.monthly_targets
    from common.metrics.daily_report import fetch_channel_monthly_targets
    return fetch_channel_monthly_targets(conn) or cfg.monthly_targets


def run_channel_missing_task(conn, outbox, **kwargs):
    from common.daily_robot.channel_missing import run_channel_missing
    return run_channel_missing(conn, outbox, **kwargs)


def run_freshness_check_task(conn, outbox, **kwargs):
    from common.daily_robot.freshness_check import run_freshness_check
    return run_freshness_check(conn, outbox, **kwargs)


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


def _resolve_region(args):
    region = getattr(args, "region", None) or (
        os.environ.get("ROBOT_REGION") or ""
    ).strip()
    return region or None


def _print_failure(code):
    print(f"status=failed code={code}")


def _resolve_business_date(args, now):
    """``--date`` 解析：YYYY-MM-DD，或特殊值 ``yesterday``（= 今天 -1 天）。

    cron 无法表达「昨天」，T+1 窗口（渠道到齐检查/日报）依赖该字面值锚定
    前一业务日；缺省仍为今天。
    """
    raw = (getattr(args, "date", None) or "").strip()
    if not raw:
        return now.date()
    if raw.lower() == "yesterday":
        return now.date() - timedelta(days=1)
    return date.fromisoformat(raw)


def _force_suffix(args, now):
    """``--force``（run-once 手动触发）：另起去重键后缀强制重发；缺省 None。

    2026-10-08 裁决「手动触发不应该被幂等」：cron 窗口已发时手动触发
    会被 already_sent 静默吞掉；--force 以 ``manual-<时间戳>`` 后缀
    入队（历史行保留作审计，不覆盖不删除），当日定时窗口仍按原键幂等。
    """
    if not getattr(args, "force", False):
        return None
    return f"manual-{now:%Y%m%d%H%M%S}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def fetch_channel_month_facts(connection, *, year, month):
    from common.daily_robot.channel_daily import (
        fetch_channel_month_facts as _fetch,
    )
    return _fetch(connection, year=year, month=month)


def build_channel_section(**kwargs):
    from common.daily_robot.channel_daily import build_channel_section as _build
    return _build(**kwargs)


def resolve_channel_date(month_facts, business_date):
    from common.daily_robot.channel_daily import (
        resolve_channel_business_date as _resolve,
    )
    return _resolve(month_facts, business_date)


def build_qudao_panels_html(conn, **kwargs):
    from common.broadcast.qudao_panels import build_qudao_panels
    return build_qudao_panels(conn, **kwargs)


#: 并入渠道播报板块的区域（榜单页 extra_panels 门）。
_PANEL_REGIONS = frozenset({"qudao"})


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
        business_date = _resolve_business_date(args, now)

        conn = connect_mart(settings)

        # 渠道日销段：业务日 = 昨日（含）之前最近一个有渠道数据的工作日
        # （店铺后台导出存在时滞，预填空行日自动回退，不报 0 元假数据）。
        #
        # 2026-11-18 线下业务线剥离：原「人员榜单段」由线下仓的
        # mart_leaderboard / leaderboard 渲染，本仓不再内嵌——渠道日报只发
        # 渠道段；人员榜在榜单页/群播报（线下仓）里呈现。
        month_facts = fetch_channel_month_facts(
            conn, year=business_date.year, month=business_date.month
        )
        # 按日合计非零判定有效数据日（预填 0 值行不算，防止"0 元假日报"）
        channel_date = resolve_channel_date(month_facts, business_date)
        if channel_date is None:
            print("status=skipped reason=no_channel_data")
            return
        # 环比基准需要播报日集合：真源 dim_calendar（全播报口径 + 人工休息日
        # override）；当月无覆盖行时回退自然日全集。
        from common.public_data.calendar_store import fetch_month_days
        from common.calendar_utils import month_days
        rows = fetch_month_days(conn, business_date.year, business_date.month)
        workdays = [row[0] for row in rows if row[1]] or month_days(
            business_date.year, business_date.month
        )
        body = build_channel_section(
            month_facts=month_facts,
            business_date=channel_date,
            workdays=workdays,
            monthly_targets=channel_monthly_targets(conn, region, cfg),
        )

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


def _handle_channel_missing(args):
    """渠道门店到齐校验（10:40，region=qudao）：缺口 @ + 零销售播报 → outbox。

    业务日默认 **昨天**（T+1 窗口）；``--dry`` 只打印名册/缺口/零销售/
    @ 名单，不入库不发消息（灰度核对用）。
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
        raw_date = (getattr(args, "date", None) or "").strip()
        business_date = (
            _resolve_business_date(args, now)
            if raw_date
            else now.date() - timedelta(days=1)
        )

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        report = {}
        outcome = run_channel_missing_task(
            conn, outbox,
            region=region, display=cfg.display,
            business_date=business_date, now=now,
            table_url=cfg.table_url,
            cc_user_ids=cfg.cc_user_ids,
            store_exclude=cfg.store_exclude,
            dry=args.dry,
            report=report,
            dedupe_suffix=_force_suffix(args, now),
        )
        if args.dry:
            print(f"roster={len(report.get('roster', []))} "
                  f"missing={len(report.get('missing', []))} "
                  f"zero={len(report.get('zero', []))}")
            for row in report.get("missing", []):
                print(f"  missing {row['channel'] or '-'} | {row['store']} "
                      f"| {'、'.join(row['owner_names']) or '-'}")
            for row in report.get("zero", []):
                print(f"  zero    {row['channel'] or '-'} | {row['store']} "
                      f"| {'、'.join(row['owner_names']) or '-'}")
            print(f"at={report.get('at_user_ids', [])} "
                  f"cc={report.get('cc_user_ids', [])} "
                  f"unmatched={report.get('unmatched', [])}")
        else:
            conn.commit()
        print(
            f"service={service_id} region={region} kind=channel_missing "
            f"status={outcome.status} missing={len(outcome.unfilled)}"
        )
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def _handle_calendar_watch(args):
    """工作日历覆盖看门（每月 25 日 10:00）：当月/下月覆盖异常发线下整体群。

    跨区域告警（投递 region 固定 offline_all，不需要 ROBOT_REGION）。
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

        now = datetime.now()
        anchor = (
            date.fromisoformat(args.date)
            if getattr(args, "date", None) else now.date()
        )

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        result = run_calendar_watch_task(
            conn, outbox, anchor_date=anchor, now=now,
            dedupe_suffix=_force_suffix(args, now),
        )
        conn.commit()
        print(f"service={service_id} kind=calendar_watch result={result}")
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def _handle_freshness_check(args):
    """L3 数据新鲜度看门狗（跨区域，投递 region=ops）：断言两表存 T-1 行。

    业务日默认 **昨天**（T-1 窗口）；``--date`` 覆盖；``--dry`` 只打印
    （不入队）；``--no-workday-gate`` 对休息日也入队（人工核对任意日期用）。
    阈值/冷却取本线注册表 ``params``（ops-web「告警设置」可调）。
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

        from common.daily_robot import inspection_alert

        now = datetime.now()
        raw_date = (getattr(args, "date", None) or "").strip()
        business_date = (
            date.fromisoformat(raw_date) if raw_date
            else now.date() - timedelta(days=1)
        )

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        result = run_freshness_check_task(
            conn, outbox,
            business_date=business_date, now=now,
            params=inspection_alert.fetch_service_params(service_id),
            dedupe_override=_force_suffix(args, now),
            dry=args.dry,
            skip_workday_gate=getattr(args, "no_workday_gate", False),
        )
        if not args.dry:
            conn.commit()
        print(f"service={service_id} kind=alert result={result}")
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
            help="business date override (YYYY-MM-DD or 'yesterday', "
                 "default: today)",
        )

    # -- channel-missing --------------------------------------------------------
    missing_sub = subparsers.add_parser(
        "channel-missing",
        help="Channel store fill-rate check + broadcast (10:40, qudao)",
    )
    missing_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    missing_sub.add_argument(
        "--region", default=None,
        help="business region (default: $ROBOT_REGION)",
    )
    missing_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    missing_sub.add_argument(
        "--date", default=None,
        help="business date override (YYYY-MM-DD or 'yesterday', "
             "default: yesterday)",
    )
    missing_sub.add_argument(
        "--dry", action="store_true", default=False,
        help="print roster/missing/zero/@ lists only; nothing enqueued",
    )
    missing_sub.add_argument(
        "--force", action="store_true", default=False,
        help="bypass the daily dedupe and resend (run-once 手动触发由调度器自动附加)",
    )

    # -- calendar-watch --------------------------------------------------------
    calendar_watch_sub = subparsers.add_parser(
        "calendar-watch",
        help="Workday-calendar coverage watch (25th 10:00, alert to offline_all)",
    )
    calendar_watch_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    calendar_watch_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    calendar_watch_sub.add_argument(
        "--date", default=None,
        help="anchor date override (YYYY-MM-DD, default: today)",
    )
    calendar_watch_sub.add_argument(
        "--force", action="store_true", default=False,
        help="bypass the daily dedupe and resend (run-once 手动触发由调度器自动附加)",
    )

    # -- freshness-check -------------------------------------------------------
    freshness_sub = subparsers.add_parser(
        "freshness-check",
        help="L3 data freshness watchdog: assert a T-1 row exists in the "
             "channel facts (alert to region 'ops')",
    )
    freshness_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    freshness_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    freshness_sub.add_argument(
        "--date", default=None,
        help="business date override (YYYY-MM-DD, default: yesterday)",
    )
    freshness_sub.add_argument(
        "--dry", action="store_true", default=False,
        help="print findings only; nothing enqueued",
    )
    freshness_sub.add_argument(
        "--no-workday-gate", action="store_true", default=False,
        help="enqueue even when T-1 is a non-workday (manual verification)",
    )
    freshness_sub.add_argument(
        "--force", action="store_true", default=False,
        help="bypass the daily dedupe and resend (run-once 手动触发由调度器自动附加)",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "channel-daily":
        _handle_channel_daily(args)
        return
    if args.command == "channel-missing":
        _handle_channel_missing(args)
        return
    if args.command == "calendar-watch":
        _handle_calendar_watch(args)
        return
    if args.command == "freshness-check":
        _handle_freshness_check(args)
        return


if __name__ == "__main__":
    main()
