"""Non-leaking CLI for restricted live public data sync.

This module must never print credentials, payloads, URLs, or tracebacks.
All output is limited to safe run-summary lines.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Lazy wrappers -- these are module-level so tests can patch them without
# importing heavy dependencies at module-load time.
# ---------------------------------------------------------------------------

def load_settings():
    """Return ``Settings`` from the process environment."""
    from common.public_data.settings import Settings
    return Settings.from_environment()


def require_live_run(settings, *, live_read, confirm_local_test_write):
    """Raise ``LiveRunRejected`` unless every safety gate passes."""
    from common.public_data.live_safety import require_live_run as _require
    return _require(
        settings,
        live_read=live_read,
        confirm_local_test_write=confirm_local_test_write,
    )


def require_extract_run(settings, *, confirm_local_test_write):
    """Raise ``LiveRunRejected`` unless the extract-run safety gates pass."""
    from common.public_data.live_safety import require_extract_run as _require
    return _require(
        settings,
        confirm_local_test_write=confirm_local_test_write,
    )


def load_manifest(path):
    """Parse and validate the source manifest at *path*."""
    from common.public_data.manifest import load_manifest as _load
    return _load(path)


def load_calendar_seed(path):
    """Parse the version-controlled workday-calendar seed at *path*."""
    from common.calendar_utils import load_calendar_seed as _load
    return _load(path)


def load_org_seed(path):
    """Parse the version-controlled region->dept org seed at *path*."""
    from common.public_data.org_read import load_org_seed as _load
    return _load(path)


def connect(database_settings):
    """Open a connection to one of the public-data databases."""
    from common.public_data.db import connect as _connect
    return _connect(database_settings)


def load_target_seed(path):
    """Parse the version-controlled annual-target seed at *path*."""
    from common.public_data.target_seed import load_target_seed as _load
    return _load(path)


def replace_dim_target(connection, rows):
    """Replay *rows* into ``dim_target`` inside one transaction."""
    from common.public_data.target_seed import replace_dim_target as _replace
    return _replace(connection, rows)


def load_grant_seed(path):
    """Parse the version-controlled ops-web grant bootstrap seed at *path*."""
    from common.public_data.bi_authz import load_grant_seed as _load
    return _load(path)


def apply_grant_seed(connection, rows, if_missing=False):
    """Upsert the grant bootstrap seed into ``bi_authz_grant`` (+ audit)."""
    from common.public_data.bi_authz import apply_grant_seed as _apply
    return _apply(connection, rows, if_missing=if_missing)


def build_pipeline_config_source():
    """Return the configured pipeline-registry config source."""
    from common.public_data.pipeline_config import build_config_source
    return build_config_source()


def resolve_service_id(override=None):
    """Return this process's pipeline service id (CLI flag wins over env)."""
    from common.public_data.pipeline_config import resolve_service_id as _resolve
    return _resolve(override=override)


def publish_pipeline_seed(seed_path, if_missing=False):
    """Publish the version-controlled pipeline seed into Nacos."""
    from common.public_data.pipeline_config import publish_seed_from_env
    return publish_seed_from_env(seed_path, if_missing=if_missing)


def publish_bi_seed(seed_path, if_missing=False):
    """Publish the version-controlled dashboard seed into Nacos."""
    from common.bi_web.config import publish_bi_seed_from_env
    return publish_bi_seed_from_env(seed_path, if_missing=if_missing)


def _pipeline_enabled(service_id):
    """True unless the registry explicitly disables *service_id*.

    Fail-open on any registry error so a flaky configuration centre never
    silently stops reports (spec section 3).
    """
    if not service_id:
        return True
    try:
        return build_pipeline_config_source().get_pipeline(service_id).enabled
    except Exception:
        return True


def load_source_credentials(path, source=None):
    """Read and validate the source-credentials JSON file at *path*.

    The file may contain::

        {
          "dingtalk": {"app_key": "...", "app_secret": "...", "operator_id": "..."},
          "wdt":      {"sid": "...",    "app_key": "...",    "app_secret": "..."}
        }

    When *source* is ``"dingtalk"`` or ``"wdt"`` only that section is
    required (per-line runners mount just their own credentials); when
    *source* is ``None`` both sections are required.

    Raises ``ValueError`` with a message that contains only
    ``"invalid source credentials"`` -- credential values are never
    interpolated into the error.
    """
    required = {
        "dingtalk": ("app_key", "app_secret", "operator_id"),
        "wdt": ("sid", "app_key", "app_secret"),
    }
    if source is not None:
        if source not in required:
            raise ValueError("invalid source credentials")
        required = {source: required[source]}

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        raise ValueError("invalid source credentials")

    if not isinstance(data, dict):
        raise ValueError("invalid source credentials")

    for section, keys in required.items():
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            raise ValueError("invalid source credentials")
        for key in keys:
            value = section_data.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError("invalid source credentials")

    return data


def build_gateways(credentials, connections):
    """Construct read gateways from *credentials* and DB *connections*.

    Returns a ``(dingtalk_gateway, wdt_gateway)`` tuple.  A gateway is
    ``None`` when its credentials section is absent, letting per-line
    runners carry only their own source's credentials; the sync service
    validates the gateway for whichever source it actually runs.
    """
    from common.public_data.dingtalk_read import DingTalkReadGateway
    from common.public_data.wdt_read import WdtReadGateway

    dingtalk_gateway = None
    dingtalk_creds = credentials.get("dingtalk")
    if dingtalk_creds:
        dingtalk_gateway = DingTalkReadGateway(
            app_key=dingtalk_creds["app_key"],
            app_secret=dingtalk_creds["app_secret"],
            operator_id=dingtalk_creds["operator_id"],
        )

    wdt_gateway = None
    wdt_creds = credentials.get("wdt")
    if wdt_creds:
        # Build real client if creds look valid, else a stub that raises.
        wdt_gateway = WdtReadGateway(call=_build_wdt_call(wdt_creds))

    return dingtalk_gateway, wdt_gateway


def build_org_gateway(credentials):
    """Construct the contact-directory gateway from DingTalk credentials.

    Returns ``None`` when the DingTalk section is absent (a wdt-only runner);
    the sync service fails loudly only if the manifest actually declares the
    directory dataset.
    """
    from common.public_data.org_read import OrgReadGateway

    dingtalk_creds = credentials.get("dingtalk")
    if not dingtalk_creds:
        return None
    return OrgReadGateway(
        app_key=dingtalk_creds["app_key"],
        app_secret=dingtalk_creds["app_secret"],
    )


def _build_wdt_call(wdt_creds):
    """Return a WdtClient.call-compatible callable, or a stub if creds are placeholders."""
    sid = wdt_creds.get("sid", "")
    app_key = wdt_creds.get("app_key", "")
    app_secret = wdt_creds.get("app_secret", "")

    # Placeholder detection: if any field looks like the example template, return stub
    if not sid or not app_key or not app_secret or "replace" in sid.lower():
        def _wdt_stub(*args, **kwargs):
            raise RuntimeError("WDT credentials are placeholders; cannot call WDT API")
        return _wdt_stub

    from common.wdt.client import WdtClient
    client = WdtClient(sid=sid, appkey=app_key, appsecret=app_secret)
    return client.call


def build_service(settings, credentials, manifest, org_regions=()):
    """Open connections, run migrations, build gateways, return a ``LiveSyncService``.

    *org_regions* comes from the version-controlled org seed (region ->
    top-level dept ids, priority-ordered); it is only consulted when the
    manifest declares the contact-directory dataset.
    """
    from common.public_data.db import connect
    from common.public_data.live_migrations import apply_live_migrations
    from common.public_data.raw_repository import (
        DingTalkRawRepository,
        OrgRawRepository,
        WdtRawRepository,
    )
    from common.public_data.mart_repository import MartRepository
    from common.public_data.live_sync import LiveSyncService

    dingtalk_conn = connect(settings.dingtalk_database)
    wdt_conn = connect(settings.wdt_database)
    mart_conn = connect(settings.mart_database)

    apply_live_migrations(dingtalk_conn, wdt_conn, mart_conn)

    class _Connections:
        pass

    conns = _Connections()
    conns.dingtalk = dingtalk_conn
    conns.wdt = wdt_conn
    conns.mart = mart_conn

    dingtalk_gateway, wdt_gateway = build_gateways(credentials, conns)

    dingtalk_repo = DingTalkRawRepository(dingtalk_conn)
    wdt_repo = WdtRawRepository(wdt_conn)
    mart_repo = MartRepository(mart_conn)

    org_gateway = None
    org_repo = None
    if manifest.dingtalk_org is not None:
        org_gateway = build_org_gateway(credentials)
        org_repo = OrgRawRepository(dingtalk_conn)

    from datetime import datetime, timezone
    return LiveSyncService(
        dingtalk_gateway=dingtalk_gateway,
        wdt_gateway=wdt_gateway,
        dingtalk_repository=dingtalk_repo,
        wdt_repository=wdt_repo,
        mart_repository=mart_repo,
        connections=conns,
        now=lambda: datetime.now(timezone.utc),
        new_run_id=lambda: str(uuid.uuid4()),
        org_gateway=org_gateway,
        org_repository=org_repo,
        org_regions=org_regions,
    )


def load_manual_template(name, directory=None):
    """Parse and validate the version-controlled manual-import template."""
    from common.public_data.manual_import.template import (
        load_template_by_name as _load,
    )
    return _load(name, directory)


def load_manual_table(path):
    """Read a manual report file (CSV/XLSX) into source rows."""
    from common.public_data.manual_import.loader import load_table as _load
    return _load(path)


def validate_manual_table(table, template, period):
    """Validate *table* against *template*; returns a structured report."""
    from common.public_data.manual_import.validate import validate_table as _validate
    return _validate(table, template, period)


def build_manual_import_service(settings=None):
    """Build the manual-import service.

    *settings* is ``None`` for a dry run: no connection is opened, no byte is
    written.  Otherwise the ``raw_manual`` + ``mart_ops`` connections are
    opened, the channel's migrations applied, and a writing service returned.
    """
    from datetime import datetime, timezone

    from common.public_data.manual_import.projector import ManualProjector
    from common.public_data.manual_import.repository import ManualImportRepository
    from common.public_data.manual_import.service import ManualImportService

    if settings is None:
        return ManualImportService(
            now=lambda: datetime.now(timezone.utc),
            new_run_id=lambda: str(uuid.uuid4()),
        )

    from common.public_data.db import connect
    from common.public_data.live_migrations import apply_manual_migrations

    manual_conn = connect(settings.manual_database)
    mart_conn = connect(settings.mart_database)
    apply_manual_migrations(manual_conn, mart_conn)
    return ManualImportService(
        repository=ManualImportRepository(manual_conn),
        projector=ManualProjector(mart_conn),
        now=lambda: datetime.now(timezone.utc),
        new_run_id=lambda: str(uuid.uuid4()),
    )


def build_extract_service(settings, calendar_months=()):
    """Open connections, run migrations, return a ``MartExtractService``.

    No source credentials are read or mounted: the extraction layer consumes
    the local ``raw_*`` databases only and never contacts a source
    (spec section 7).  *calendar_months* comes from the version-controlled
    seed; an empty tuple simply skips the ``dim_calendar`` step.
    """
    from datetime import datetime, timezone

    from common.public_data.db import connect
    from common.public_data.extract_mart import (
        MartExtractRepository,
        MartExtractService,
    )
    from common.public_data.live_migrations import apply_live_migrations
    from common.public_data.mart_repository import MartRepository

    dingtalk_conn = connect(settings.dingtalk_database)
    wdt_conn = connect(settings.wdt_database)
    mart_conn = connect(settings.mart_database)

    apply_live_migrations(dingtalk_conn, wdt_conn, mart_conn)

    return MartExtractService(
        repository=MartExtractRepository(dingtalk_conn, mart_conn, wdt_connection=wdt_conn),
        mart_repository=MartRepository(mart_conn),
        mart_connection=mart_conn,
        now=lambda: datetime.now(timezone.utc),
        new_run_id=lambda: str(uuid.uuid4()),
        calendar_months=calendar_months,
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _get_attr_or_key(obj, name, default=None):
    """Read *name* from a dict or dataclass-like object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _print_success(result):
    run_id = _get_attr_or_key(result, "run_id", "unknown")
    datasets = _get_attr_or_key(result, "datasets", [])
    for ds in datasets:
        source = _get_attr_or_key(ds, "source", "unknown")
        dataset = _get_attr_or_key(ds, "dataset", "unknown")
        records_read = _get_attr_or_key(ds, "records_read", 0)
        raw_written = _get_attr_or_key(ds, "raw_records_written", 0)
        digest = _get_attr_or_key(ds, "record_id_digest", "unknown")
        print(
            f"run_id={run_id} source={source} dataset={dataset}"
            f" records_read={records_read} raw_records_written={raw_written}"
            f" record_id_digest={digest} status=completed"
        )


def _print_failure(run_id="unknown", code="sync_error"):
    print(f"run_id={run_id} status=failed code={code}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _handle_live_sync(args):
    # Safety pre-check: both flags must be present BEFORE any other work.
    if not args.live_read or not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_live_run(
            settings,
            live_read=args.live_read,
            confirm_local_test_write=args.confirm_local_test_write,
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return
        manifest = load_manifest(settings.source_config_path)
        credentials = load_source_credentials(
            args.source_credentials, source=args.source
        )
        org_regions = ()
        org_seed_path = getattr(settings, "org_seed_path", None)
        if manifest.dingtalk_org is not None and isinstance(org_seed_path, Path):
            org_regions = load_org_seed(org_seed_path)
        service = build_service(settings, credentials, manifest, org_regions)
        result = service.sync(manifest, source=args.source)
        _print_success(result)
    except SystemExit:
        raise
    except Exception:
        _print_failure()
        sys.exit(1)


def _handle_rebuild_projection(args):
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_live_run(
            settings,
            live_read=False,
            confirm_local_test_write=args.confirm_local_test_write,
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return
        manifest = load_manifest(settings.source_config_path)
        service = build_service(settings, {}, manifest)
        result = service.rebuild_projection(
            args.sync_run_id, manifest, source=args.source
        )
        _print_success(result)
    except SystemExit:
        raise
    except Exception:
        _print_failure(run_id=getattr(args, "sync_run_id", "unknown"))
        sys.exit(1)


def _handle_extract_mart(args):
    # Safety pre-check: the write-confirmation flag is required BEFORE any work.
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_extract_run(
            settings,
            confirm_local_test_write=args.confirm_local_test_write,
        )
        service_id = resolve_service_id(getattr(args, "service", None))
        if not _pipeline_enabled(service_id):
            print(f"service={service_id} status=skipped reason=disabled")
            return

        calendar_months = ()
        seed_path = getattr(settings, "calendar_seed_path", None)
        if isinstance(seed_path, Path):
            calendar_months = tuple(load_calendar_seed(seed_path))

        service = build_extract_service(settings, calendar_months)
        result = service.extract()
        _print_success(result)
    except SystemExit:
        raise
    except Exception:
        _print_failure()
        sys.exit(1)


def _handle_publish_pipelines(args):
    try:
        count = publish_pipeline_seed(args.seed, if_missing=args.if_missing)
        print(f"published={count} pipelines")
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="config_error")
        sys.exit(1)


def _handle_publish_bi(args):
    try:
        count = publish_bi_seed(args.seed, if_missing=args.if_missing)
        print(f"published={count} dashboards")
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="config_error")
        sys.exit(1)


def _handle_migrate(args):
    try:
        settings = load_settings()
        from common.public_data.db import connect
        from common.public_data.live_migrations import apply_live_migrations

        dingtalk_conn = connect(settings.dingtalk_database)
        wdt_conn = connect(settings.wdt_database)
        mart_conn = connect(settings.mart_database)
        manual_conn = (
            connect(settings.manual_database)
            if settings.manual_database is not None
            else None
        )
        apply_live_migrations(
            dingtalk_conn, wdt_conn, mart_conn, manual_connection=manual_conn
        )
        print("migrations applied")
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="migration_error")
        sys.exit(1)


def _handle_load_target(args):
    # Safety pre-check: the write-confirmation flag is required BEFORE any work.
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        require_extract_run(
            settings,
            confirm_local_test_write=args.confirm_local_test_write,
        )
        # Fail fast on a bad seed before any connection is opened.
        rows = load_target_seed(args.seed)
        from common.public_data.live_migrations import apply_live_migrations

        dingtalk_conn = connect(settings.dingtalk_database)
        wdt_conn = connect(settings.wdt_database)
        mart_conn = connect(settings.mart_database)
        apply_live_migrations(dingtalk_conn, wdt_conn, mart_conn)
        written = replace_dim_target(mart_conn, rows)
        print(f"targets_written={written} status=completed")
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="target_seed_error")
        sys.exit(1)


def _handle_load_ops_seed(args):
    # Safety pre-check: the write-confirmation flag is required BEFORE any work.
    if not args.confirm_local_test_write:
        sys.exit(1)

    try:
        settings = load_settings()
        # Fail fast on a bad seed before any connection is opened.
        rows = load_grant_seed(args.seed)
        from common.public_data.live_migrations import apply_live_migrations

        dingtalk_conn = connect(settings.dingtalk_database)
        wdt_conn = connect(settings.wdt_database)
        mart_conn = connect(settings.mart_database)
        apply_live_migrations(dingtalk_conn, wdt_conn, mart_conn)
        written = apply_grant_seed(mart_conn, rows, if_missing=args.if_missing)
        print(f"grants_written={written} status=completed")
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="ops_seed_error")
        sys.exit(1)


def _handle_import_manual(args):
    """人工报表导入：默认 dry-run 只打印校验报告，``--apply`` 才落库。"""
    try:
        template = load_manual_template(args.template, args.templates_dir)
        table = load_manual_table(args.file)
        settings = load_settings() if args.apply else None
        service = build_manual_import_service(settings)
        if args.apply:
            result = service.apply(
                template, table, args.period, imported_by=args.imported_by
            )
        else:
            result = service.dry_run(template, table, args.period)

        for line in result.report.summary_lines():
            print(line)
        print(
            f"run_id={result.run_id} dataset={result.dataset}"
            f" period={result.period} rows_ok={result.rows_ok}"
            f" rows_bad={result.rows_bad} status={result.status}"
        )
        if not result.report.ok:
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        _print_failure(code="manual_import_error")
        sys.exit(1)


def _handle_status(args):
    print("status: ok")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="public-data",
        description="Restricted live public data sync CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- migrate -------------------------------------------------------------
    subparsers.add_parser("migrate", help="Apply live migrations")

    # -- status --------------------------------------------------------------
    subparsers.add_parser("status", help="Show sync run status")

    # -- live-sync -----------------------------------------------------------
    live_sync = subparsers.add_parser("live-sync", help="Run live sync")
    live_sync.add_argument("--live-read", action="store_true", default=False)
    live_sync.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    live_sync.add_argument("--source-credentials", required=True)
    live_sync.add_argument(
        "--source", choices=("dingtalk", "wdt"), default=None,
        help="restrict the run to a single source line (default: both)",
    )
    live_sync.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )

    # -- rebuild-projection --------------------------------------------------
    rebuild = subparsers.add_parser(
        "rebuild-projection", help="Rebuild mart projection for a sync run"
    )
    rebuild.add_argument("sync_run_id")
    rebuild.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    rebuild.add_argument(
        "--source", choices=("dingtalk", "wdt"), default=None,
        help="restrict the rebuild to a single source line (default: both)",
    )
    rebuild.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )

    # -- extract-mart --------------------------------------------------------
    extract = subparsers.add_parser(
        "extract-mart", help="Project raw tables into the mart"
    )
    extract.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )
    extract.add_argument(
        "--service", default=None,
        help="pipeline service id for the registry enable gate "
             "(default: $PUBLIC_DATA_SERVICE_ID)",
    )

    # -- publish-pipelines ---------------------------------------------------
    publish = subparsers.add_parser(
        "publish-pipelines", help="Publish the pipeline seed to Nacos"
    )
    publish.add_argument("--seed", required=True)
    publish.add_argument("--if-missing", action="store_true", default=False)

    # -- publish-bi -----------------------------------------------------------
    publish_bi = subparsers.add_parser(
        "publish-bi", help="Publish the dashboard seed to Nacos"
    )
    publish_bi.add_argument(
        "--seed", default="docker/integration/bi.seed.yaml",
        help="version-controlled dashboard seed file",
    )
    publish_bi.add_argument("--if-missing", action="store_true", default=False)

    # -- load-target ----------------------------------------------------------
    load_target = subparsers.add_parser(
        "load-target", help="Replay the annual-target seed into dim_target"
    )
    load_target.add_argument(
        "--seed", default="docker/integration/target.seed.json",
        help="version-controlled annual-target seed file",
    )
    load_target.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )

    # -- load-ops-seed --------------------------------------------------------
    load_ops_seed = subparsers.add_parser(
        "load-ops-seed",
        help="Replay the ops-web grant bootstrap seed into bi_authz_grant",
    )
    load_ops_seed.add_argument(
        "--seed", default="docker/integration/ops.seed.yaml",
        help="version-controlled grant bootstrap seed file",
    )
    load_ops_seed.add_argument(
        "--if-missing", action="store_true", default=False,
        help="skip grant records that already exist (default: overwrite)",
    )
    load_ops_seed.add_argument(
        "--confirm-local-test-write", action="store_true", default=False
    )

    # -- import-manual -------------------------------------------------------
    import_manual = subparsers.add_parser(
        "import-manual", help="Validate and import a manual report file"
    )
    import_manual.add_argument(
        "--template", required=True,
        help="template name under docker/integration/manual-import-templates",
    )
    import_manual.add_argument("--file", required=True, help="CSV or XLSX report file")
    import_manual.add_argument(
        "--period", required=True,
        help="period matching the template: YYYY-MM / YYYY-Qn / YYYY",
    )
    import_manual.add_argument(
        "--apply", action="store_true", default=False,
        help="write to raw_manual + mart_ops (default: dry-run report only)",
    )
    import_manual.add_argument("--imported-by", default="cli")
    import_manual.add_argument(
        "--templates-dir", default=None,
        help="template directory (default: docker/integration/manual-import-templates)",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "live-sync": _handle_live_sync,
        "rebuild-projection": _handle_rebuild_projection,
        "extract-mart": _handle_extract_mart,
        "publish-pipelines": _handle_publish_pipelines,
        "publish-bi": _handle_publish_bi,
        "migrate": _handle_migrate,
        "load-target": _handle_load_target,
        "load-ops-seed": _handle_load_ops_seed,
        "import-manual": _handle_import_manual,
        "status": _handle_status,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
