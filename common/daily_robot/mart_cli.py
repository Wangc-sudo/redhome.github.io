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


def run_offline_daily_task(conn, outbox, **kwargs):
    from common.daily_robot.offline_summary import run_daily_summary
    return run_daily_summary(conn, outbox, **kwargs)


def run_target_rollover_task(conn, **kwargs):
    from common.daily_robot.target_rollover import run_rollover
    return run_rollover(conn, **kwargs)


def run_target_remind_task(conn, outbox, **kwargs):
    from common.daily_robot.target_rollover import run_remind
    return run_remind(conn, outbox, **kwargs)


def run_offline_weekly_task(conn, outbox, **kwargs):
    from common.daily_robot.offline_summary import run_weekly_summary
    return run_weekly_summary(conn, outbox, **kwargs)


def run_offline_monthly_task(conn, outbox, **kwargs):
    from common.daily_robot.offline_summary import run_monthly_summary
    return run_monthly_summary(conn, outbox, **kwargs)


def mart_collect_data(conn, *, region, business_date):
    from common.daily_robot.mart_leaderboard import mart_collect
    return mart_collect(conn, region=region, business_date=business_date)


def build_view(region_cfg, workdays, *, year, month):
    from common.daily_robot.mart_leaderboard import build_leaderboard_view
    return build_leaderboard_view(region_cfg, workdays, year=year, month=month)


def render_bc(region, calendar, now, elapsed, people, url=None,
              dept_overrides=None):
    from common.daily_robot.leaderboard import render_bc_markdown
    return render_bc_markdown(
        region, calendar, now, elapsed, people, url=url,
        dept_overrides=dept_overrides,
    )


def build_html_page(view, now, elapsed, people, extra_panels=None,
                    dept_overrides=None):
    from common.daily_robot.leaderboard import build_html
    return build_html(
        view, now, elapsed, people, extra_panels=extra_panels,
        dept_overrides=dept_overrides,
    )


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


def build_offline_all_page(conn, cfg, *, business_date, now):
    from common.daily_robot.offline_summary import build_offline_all_html
    return build_offline_all_html(conn, cfg, business_date=business_date, now=now)


def run_channel_missing_task(conn, outbox, **kwargs):
    from common.daily_robot.channel_missing import run_channel_missing
    return run_channel_missing(conn, outbox, **kwargs)


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


def _force_suffix(args, now):
    """``--force``（run-once 手动触发）：另起去重键后缀强制重发；缺省 None。

    2026-10-08 裁决「手动触发不应该被幂等」：cron 窗口已发时手动触发
    会被 already_sent 静默吞掉；--force 以 ``manual-<时间戳>`` 后缀
    入队（历史行保留作审计，不覆盖不删除），当日定时窗口仍按原键幂等。
    """
    if not getattr(args, "force", False):
        return None
    return f"manual-{now:%Y%m%d%H%M%S}"


def _resolve_business_date(args, now):
    """``--date`` 解析：YYYY-MM-DD，或特殊值 ``yesterday``（= 今天 -1 天）。

    cron 无法表达「昨天」，T+1 窗口（10:31 采集 / 10:45 页面重算）
    依赖该字面值锚定前一业务日；缺省仍为今天。
    """
    raw = (getattr(args, "date", None) or "").strip()
    if not raw:
        return now.date()
    if raw.lower() == "yesterday":
        return now.date() - timedelta(days=1)
    return date.fromisoformat(raw)


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
        business_date = _resolve_business_date(args, now)
        dedupe_suffix = _force_suffix(args, now)

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
                    dedupe_suffix=dedupe_suffix,
                )
            else:
                outcome = run_check_task(
                    conn, outbox,
                    region=region, display=cfg.display,
                    table_url=cfg.table_url,
                    business_date=business_date, now=now,
                    cc_user_ids=cfg.cc_user_ids, aliases=cfg.aliases,
                    dedupe_suffix=dedupe_suffix,
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
        business_date = _resolve_business_date(args, now)

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
            dept_overrides=channel_dept_overrides(conn, region, data),
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
        data = mart_collect_data(conn, region=region, business_date=business_date)
        view = build_view(
            cfg, data.workdays,
            year=business_date.year, month=business_date.month,
        )
        people_body = render_bc(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people),
            url=cfg.leaderboard_url or None,
            dept_overrides=channel_dept_overrides(conn, region, data),
        )

        # 渠道日销段：业务日 = 昨日（含）之前最近一个有渠道数据的工作日
        # （店铺后台导出存在时滞，预填空行日自动回退，不报 0 元假数据）。
        section = ""
        month_facts = fetch_channel_month_facts(
            conn, year=business_date.year, month=business_date.month
        )
        # 按日合计非零判定有效数据日（预填 0 值行不算，防止"0 元假日报"）
        channel_date = resolve_channel_date(month_facts, business_date)
        if channel_date is not None:
            section = build_channel_section(
                month_facts=month_facts,
                business_date=channel_date,
                workdays=data.workdays,
                monthly_targets=channel_monthly_targets(conn, region, cfg),
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


def _handle_offline_summary(args, *, period):
    """线下整体汇总（offline_all 群）：daily/weekly/monthly 三周期 → outbox。

    kind 与业务日：daily=当日；weekly=上周周一；monthly=上月 1 日——
    幂等键自带周期唯一性，重跑不重复发。
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
        if configs.get(region) is None:
            _print_failure("unknown_region")
            sys.exit(1)

        now = datetime.now()
        reference = (
            date.fromisoformat(args.date) if getattr(args, "date", None)
            else now.date()
        )

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        dedupe_suffix = _force_suffix(args, now)
        if period == "daily":
            status = run_offline_daily_task(
                conn, outbox, business_date=reference, now=now,
                dedupe_suffix=dedupe_suffix,
            )
        elif period == "weekly":
            status = run_offline_weekly_task(
                conn, outbox, reference=reference, now=now,
                dedupe_suffix=dedupe_suffix,
            )
        else:
            status = run_offline_monthly_task(
                conn, outbox, reference=reference, now=now,
                dedupe_suffix=dedupe_suffix,
            )
        conn.commit()
        print(
            f"service={service_id} region={region} kind=offline_{period} "
            f"status={status}"
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


def _handle_target_rollover(args):
    """月目标结转（每月 1 日 06:00）：上月 dim_report_target 幂等结转到当月。

    跨区域数据任务（不绑 region、不发消息）；``--dry`` 只打印将结转/
    将跳过的名单，不写库（人工已录入的键永不覆盖）。
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
        result = run_target_rollover_task(conn, anchor_date=anchor,
                                          apply=not args.dry)
        print(
            f"service={service_id} kind=target_rollover "
            f"from={result['from_month']} to={result['to_month']} "
            f"inserted={len(result['inserted'])} "
            f"skipped={len(result['skipped'])} "
            f"status={'dry' if args.dry else 'carried'}"
        )
        for scope, name in result["inserted"]:
            print(f"  {'would-carry' if args.dry else 'carried'} "
                  f"{scope} | {name}")
        for scope, name in result["skipped"]:
            print(f"  kept-manual {scope} | {name}")
    except SystemExit:
        raise
    except Exception:
        _print_failure("robot_error")
        sys.exit(1)


def _handle_target_remind(args):
    """月目标核对提醒（每月 28 日 10:00）：当月有目标的区域群各一条。

    跨区域播报（region 来自各 scope 自身，不需要 ROBOT_REGION）。
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

        seed_path = getattr(settings, "region_seed_path", None)
        if seed_path is None:
            _print_failure("region_seed_required")
            sys.exit(1)
        configs = load_region_configs(seed_path)

        now = datetime.now()
        anchor = (
            date.fromisoformat(args.date)
            if getattr(args, "date", None) else now.date()
        )

        conn = connect_mart(settings)
        outbox = build_outbox(conn)
        results = run_target_remind_task(
            conn, outbox, region_configs=configs, anchor_date=anchor, now=now,
            dedupe_suffix=_force_suffix(args, now),
        )
        conn.commit()
        sent = sum(1 for _, status in results if status == "enqueued")
        print(
            f"service={service_id} kind=target_remind "
            f"scopes={len(results)} enqueued={sent}"
        )
        for scope, status in results:
            print(f"  {status} {scope}")
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
        business_date = _resolve_business_date(args, now)

        conn = connect_mart(settings)
        if region == "offline_all":
            # 线下整体无 region=offline_all 的事实行：板块榜由
            # offline_summary 聚合生成（杭州/绍兴/省外/总经办/李树军为行）。
            page = build_offline_all_page(
                conn, cfg, business_date=business_date, now=now
            )
            stats = "scopes=5"
        else:
            data = mart_collect_data(conn, region=region, business_date=business_date)
            view = build_view(
                cfg, data.workdays,
                year=business_date.year, month=business_date.month,
            )

            # 渠道播报板块（2026-09-23：qudao 群日报类播报并入页面、停单独
            # 播报）。板块生成 fail-open：任一板块失败只降级为占位，整页必须
            # 照常产出（详见 common.broadcast.qudao_panels）。
            extra_panels = None
            if region in _PANEL_REGIONS:
                extra_panels = build_qudao_panels_html(
                    conn,
                    business_date=business_date,
                    now=now,
                    workdays=data.workdays,
                    monthly_targets=channel_monthly_targets(conn, region, cfg),
                )
            page = build_html_page(
                view, now, list(data.elapsed), list(data.people),
                extra_panels=extra_panels,
                dept_overrides=channel_dept_overrides(conn, region, data),
            )
            stats = f"people={len(data.people)}" + (
                f" panels={len(extra_panels)}" if extra_panels is not None else ""
            )

        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(page, encoding="utf-8")
        print(
            f"service={service_id} region={region} kind=leaderboard-html "
            f"status=written {stats}"
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
        ("offline-daily", "Enqueue the offline-all daily summary (20:30)"),
        ("offline-weekly", "Enqueue the offline-all weekly summary (Mon 09:30)"),
        ("offline-monthly", "Enqueue the offline-all monthly summary (1st 10:00)"),
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
        if name in (
            "remind", "check", "once",
            "offline-daily", "offline-weekly", "offline-monthly",
        ):
            sub.add_argument(
                "--force", action="store_true", default=False,
                help="bypass the daily dedupe and resend (run-once 手动触发 "
                     "由调度器自动附加；leaderboard/channel-daily 自带时分 "
                     "后缀天然可重跑，无此 flag)",
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

    # -- target-rollover -------------------------------------------------------
    rollover_sub = subparsers.add_parser(
        "target-rollover",
        help="Carry last month's dim_report_target into the current month "
             "(1st 06:00, idempotent, manual entries win)",
    )
    rollover_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    rollover_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    rollover_sub.add_argument(
        "--date", default=None,
        help="anchor date override (YYYY-MM-DD, default: today); "
             "its month is the carry target",
    )
    rollover_sub.add_argument(
        "--dry", action="store_true", default=False,
        help="print the would-be carry/skip lists only; nothing written",
    )

    # -- target-remind ---------------------------------------------------------
    target_remind_sub = subparsers.add_parser(
        "target-remind",
        help="Month-end target verification reminder to region groups "
             "(28th 10:00)",
    )
    target_remind_sub.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    target_remind_sub.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )
    target_remind_sub.add_argument(
        "--date", default=None,
        help="anchor date override (YYYY-MM-DD, default: today)",
    )
    target_remind_sub.add_argument(
        "--force", action="store_true", default=False,
        help="bypass the daily dedupe and resend (run-once 手动触发由调度器自动附加)",
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
    if args.command in ("offline-daily", "offline-weekly", "offline-monthly"):
        _handle_offline_summary(args, period=args.command.split("-", 1)[1])
        return
    if args.command == "channel-missing":
        _handle_channel_missing(args)
        return
    if args.command == "target-rollover":
        _handle_target_rollover(args)
        return
    if args.command == "target-remind":
        _handle_target_remind(args)
        return
    if args.command == "leaderboard-html":
        _handle_leaderboard_html(args)
        return

    _handle(args, kind=None if args.command == "once" else args.command)


if __name__ == "__main__":
    main()
