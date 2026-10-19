"""舰队月检（fleet-audit，2026-10-17 治理 P2，用户裁决「产出进画布不发群」）。

每月 2 日 07:00 对舰队做四节体检并把快照落 ``pd_ops_fleet_audit``
（append-only，按 audit_date 幂等覆盖），ops-web 治理抽屉「上次月检快照」
节读最新一行。四节口径与前端 ``flow/governance.js`` 纯函数一致（两侧测试
用同场景夹具钉住防漂移）：

* 停用盘点：停用带原因/复查日，复查过期置顶；
* 僵尸任务：启用且可调度（has_command）但超周期宽限无真实 cron 运行
  （真源 pd_ops_run_history；daemon / _UNSCHEDULABLE 工具跳过——
  2026-10-17 复核裁决，0 真僵尸事故的口径修复）；
* 装饰依赖线：depends_on 引用未注册探针的上游（门禁不生效）；
* cron 堆积：同一时刻（HH:MM）启用任务 ≥ 3 条。

运行方式：``python -m common.public_data.fleet_audit`` 缺省 dry-run 只打印；
``--confirm-local-test-write`` 才落库（与全仓写面旗标一致）。
"""

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from common.public_data.db import connect, transaction

logger = logging.getLogger(__name__)

#: 僵尸宽限按调度周期分级（与 governance.js ZOMBIE_GRACE 同值）：
#: 日检 7 天、周检 9 天、月检 32 天。
GRACE_DAILY = 7
GRACE_WEEKLY = 9
GRACE_MONTHLY = 32

#: 有摘要/文件探针的上游（依赖门禁真生效）。真源 = scheduler
#: _DEP_SUMMARY_PROBES 键 ∪ {roll-manifest}；与 governance.js
#: PROBED_UPSTREAMS 同集（两侧测试钉住）。
PROBED_UPSTREAMS = frozenset({
    "roll-manifest",
    "sync-channel-sales",
    "extract-channel",
    "extract-mart",
})

_CRON_PILEUP_THRESHOLD = 3

_FLEET_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_ops_fleet_audit` (\n"
    "  `id` BIGINT NOT NULL AUTO_INCREMENT,\n"
    "  `audit_date` DATE NOT NULL,\n"
    "  `total` INT NOT NULL,\n"
    "  `enabled` INT NOT NULL,\n"
    "  `disabled_count` INT NOT NULL,\n"
    "  `zombie_count` INT NOT NULL,\n"
    "  `decorative_dep_count` INT NOT NULL,\n"
    "  `pileup_count` INT NOT NULL,\n"
    "  `report` JSON NOT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`id`),\n"
    "  UNIQUE KEY `uq_audit_date` (`audit_date`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def fleet_audit_ddl_statements() -> tuple:
    """返回 fleet-audit 快照表 DDL（独立迁移版本，校验和红线）。"""
    return (_FLEET_AUDIT_DDL,)


# ---------------------------------------------------------------------------
# 四节体检（纯函数，无 IO；configs = PipelineConfig 列表）
# ---------------------------------------------------------------------------

def grace_days(schedule):
    """cron 表达式 → 僵尸宽限天数（月检 32 / 周检 9 / 日检 7）。

    与 JS graceDays(schedule_zh) 语义等价：日次字段非 *=月检；
    星期字段非 *=周检；否则日检。解析不出按日检兜底。
    """
    parts = (schedule or "").split()
    if len(parts) >= 5:
        if parts[2] != "*":
            return GRACE_MONTHLY
        if parts[4] != "*":
            return GRACE_WEEKLY
    return GRACE_DAILY


def disabled_audit(configs, today):
    """停用盘点：复查过期（review_after < today）置顶。"""
    rows = []
    for cfg in configs:
        if cfg.enabled:
            continue
        review_after = cfg.review_after or ""
        rows.append({
            "service_id": cfg.service_id,
            "reason": cfg.disabled_reason or "",
            "review_after": review_after,
            "overdue": bool(review_after and review_after < today),
        })
    rows.sort(key=lambda r: (not r["overdue"], r["service_id"]))
    return rows


def zombie_audit(configs, today, latest, has_command):
    """僵尸任务：启用、可调度、超宽限无真实 cron 运行。

    ``latest`` = {service_id: 最近运行 started_at(datetime, UTC 或 None)}；
    ``has_command`` = service_id → bool（不可调度的 daemon/修复工具跳过）。
    从未跑过只在日检级宽限下判定（月/周检首期未到属正常）。
    """
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    rows = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        if not has_command(cfg.service_id):
            continue
        grace = grace_days(cfg.schedule)
        started = latest.get(cfg.service_id)
        if started is None:
            if grace <= GRACE_DAILY:
                rows.append({"service_id": cfg.service_id, "last": None,
                             "age_days": None})
            continue
        if getattr(started, "tzinfo", None) is not None:
            started = started.replace(tzinfo=None)
        age = (today_date - started.date()).days
        if age > grace:
            rows.append({
                "service_id": cfg.service_id,
                "last": started.strftime("%Y-%m-%d %H:%M"),
                "age_days": age,
            })
    rows.sort(key=lambda r: -(r["age_days"] or 9999))
    return rows


def decorative_deps(configs, probed=PROBED_UPSTREAMS):
    """装饰依赖线：depends_on 引用未注册探针的上游。"""
    rows = []
    for cfg in configs:
        fake = [d for d in (cfg.depends_on or ()) if d not in probed]
        if fake:
            rows.append({"service_id": cfg.service_id, "deps": fake})
    return rows


_HM_RE = re.compile(r"^(\d{1,2})$")


def cron_pileup(configs, threshold=_CRON_PILEUP_THRESHOLD):
    """cron 堆积：同一时刻（HH:MM）启用任务数 ≥ threshold。"""
    by_time = {}
    for cfg in configs:
        if not cfg.enabled:
            continue
        parts = (cfg.schedule or "").split()
        if len(parts) < 5:
            continue
        minute, hour = parts[0], parts[1]
        if not (_HM_RE.match(minute) and _HM_RE.match(hour)):
            continue
        key = f"{int(hour):02d}:{int(minute):02d}"
        by_time.setdefault(key, []).append(cfg.service_id)
    rows = [
        {"time": key, "count": len(ids), "services": sorted(ids)}
        for key, ids in by_time.items()
        if len(ids) >= threshold
    ]
    rows.sort(key=lambda r: -r["count"])
    return rows


def build_report(configs, today, latest, has_command,
                 probed=PROBED_UPSTREAMS):
    """四节装配（与 governance.js governanceReport 同构）。"""
    disabled = disabled_audit(configs, today)
    zombies = zombie_audit(configs, today, latest, has_command)
    deps = decorative_deps(configs, probed)
    pileups = cron_pileup(configs)
    return {
        "date": today,
        "total": len(configs),
        "enabled": sum(1 for c in configs if c.enabled),
        "disabled": disabled,
        "zombies": zombies,
        "decorative_deps": deps,
        "pileups": pileups,
    }


# ---------------------------------------------------------------------------
# 快照存取（pd_ops_fleet_audit；写方=fleet-audit 管道，读方=ops-web）
# ---------------------------------------------------------------------------

def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def save_snapshot(connection, report):
    """落一行月检快照；同日重跑按 audit_date 幂等覆盖（不重复建行）。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `pd_ops_fleet_audit` "
                "(`audit_date`, `total`, `enabled`, `disabled_count`, "
                " `zombie_count`, `decorative_dep_count`, `pileup_count`, "
                " `report`, `created_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`total`=VALUES(`total`), `enabled`=VALUES(`enabled`), "
                "`disabled_count`=VALUES(`disabled_count`), "
                "`zombie_count`=VALUES(`zombie_count`), "
                "`decorative_dep_count`=VALUES(`decorative_dep_count`), "
                "`pileup_count`=VALUES(`pileup_count`), "
                "`report`=VALUES(`report`), `created_at`=VALUES(`created_at`)",
                (
                    report["date"], report["total"], report["enabled"],
                    len(report["disabled"]), len(report["zombies"]),
                    len(report["decorative_deps"]), len(report["pileups"]),
                    json.dumps(report, ensure_ascii=False),
                    _utc_now_text(),
                ),
            )
    finally:
        cursor.close()


def fetch_latest_snapshot(connection):
    """最近一次月检快照（无 → None）；report JSON 解析为 dict。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `audit_date`, `total`, `enabled`, `disabled_count`, "
            "`zombie_count`, `decorative_dep_count`, `pileup_count`, "
            "`report`, `created_at` FROM `pd_ops_fleet_audit` "
            "ORDER BY `audit_date` DESC, `id` DESC LIMIT 1"
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return None
    get = row.get if isinstance(row, dict) else None
    if get is None:
        keys = ("audit_date", "total", "enabled", "disabled_count",
                "zombie_count", "decorative_dep_count", "pileup_count",
                "report", "created_at")
        row = dict(zip(keys, row))
        get = row.get
    report = get("report")
    if isinstance(report, (bytes, str)):
        report = json.loads(report)
    return {
        "audit_date": str(get("audit_date")),
        "total": get("total"),
        "enabled": get("enabled"),
        "disabled_count": get("disabled_count"),
        "zombie_count": get("zombie_count"),
        "decorative_dep_count": get("decorative_dep_count"),
        "pileup_count": get("pileup_count"),
        "report": report,
        "created_at": str(get("created_at")),
    }


# ---------------------------------------------------------------------------
# 装配与 CLI
# ---------------------------------------------------------------------------

def collect_report(connect_fn, database_settings, *, today,
                   fleet_source, config_source, has_command):
    """拉舰队/注册表/运行史，装配四节报告（依赖注入，测试喂 fake）。"""
    from common.public_data import ops_control

    configs = [config_source.get_pipeline(sid) for sid in fleet_source()]
    with connect_fn(database_settings) as connection:
        history = ops_control.fetch_run_history(connection, limit=1000)
    latest = {}
    for row in history:  # id DESC，首见即最新
        get = row.get if isinstance(row, dict) else None
        sid = get("service_id") if get else row[1]
        if sid not in latest:
            latest[sid] = get("started_at") if get else row[6]
    return build_report(configs, today, latest, has_command)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fleet-audit",
        description="舰队月检：四节体检快照落 pd_ops_fleet_audit（不发群）",
    )
    parser.add_argument("--confirm-local-test-write", action="store_true",
                        help="缺省 dry-run 只打印；带此旗标才落库")
    parser.add_argument("--date", default=None,
                        help="审计日（YYYY-MM-DD，缺省=今天，北京时区）")
    args = parser.parse_args(argv)

    from common.public_data.pipeline_config import build_config_source
    from common.public_data.scheduler import build_fleet_source, has_command
    from common.public_data.settings import Settings

    today = args.date or (
        datetime.now(timezone.utc) + timedelta(hours=8)
    ).strftime("%Y-%m-%d")
    settings = Settings.from_environment()
    report = collect_report(
        connect, settings.mart_database, today=today,
        fleet_source=build_fleet_source(),
        config_source=build_config_source(),
        has_command=has_command,
    )
    summary = {
        "date": report["date"], "total": report["total"],
        "enabled": report["enabled"],
        "disabled": len(report["disabled"]),
        "zombies": len(report["zombies"]),
        "decorative_deps": len(report["decorative_deps"]),
        "pileups": len(report["pileups"]),
    }
    if not args.confirm_local_test_write:
        print("fleet-audit dry-run（未落库）:", json.dumps(
            summary, ensure_ascii=False))
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    with connect(settings.mart_database) as connection:
        save_snapshot(connection, report)
    print("fleet-audit snapshot saved:", json.dumps(
        summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
