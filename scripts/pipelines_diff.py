#!/usr/bin/env python3
"""Read-only reconciliation: Nacos PIPELINES group vs the versioned seed.

Nacos is authoritative for the pipeline registry; the seed file is only the
fallback and fresh-environment default. The two drift apart whenever an
operational change lands in Nacos without a seed mirror (16 divergences were
found on 2026-10-13). This tool makes the drift visible so it can be
reconciled deliberately instead of discovered by accident.

It never writes anything -- Nacos entries are only read, the seed only
parsed. Field comparison is semantic: both sides go through
``parse_pipeline_config``, so comments/formatting never produce false
positives. ``description`` is excluded on purpose (cosmetic drift is not
operational drift).

Usage (inside dops-scheduler, or anywhere with the Nacos env set):

    python3 scripts/pipelines_diff.py [seed-path]

Exit code 0 = fully aligned, 1 = divergence found (suitable as a gate).

Environment: PUBLIC_DATA_NACOS_SERVER / _NAMESPACE / _USERNAME / _PASSWORD
(/ _GROUP, default PIPELINES) -- the same contract the scheduler uses.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from common.public_data.nacos_client import build_nacos_client  # noqa: E402
from common.public_data.pipeline_config import (  # noqa: E402
    DEFAULT_GROUP,
    parse_pipeline_config,
)

COMPARE_FIELDS = (
    "enabled",
    "kind",
    "category",
    "sources",
    "schedule",
    "reads",
    "depends_on",
)

DEFAULT_SEED = "docker/integration/pipelines.seed.yaml"


def load_seed(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {sid: parse_pipeline_config(sid, body) for sid, body in data.items()}


def load_nacos(client, group):
    list_data_ids = getattr(client, "list_data_ids", None)
    if list_data_ids is None:
        raise RuntimeError(
            "nacos client has no list_data_ids (legacy SDK path); "
            "authenticated access is required for enumeration"
        )
    entries = {}
    for data_id in list_data_ids(group):
        if not data_id.endswith(".yaml"):
            continue
        sid = data_id[: -len(".yaml")]
        raw = client.get_config(data_id, group)
        body = yaml.safe_load(raw) if raw else None
        entries[sid] = parse_pipeline_config(sid, body)
    return entries


def diff_fields(seed_cfg, nacos_cfg):
    diffs = {}
    for field in COMPARE_FIELDS:
        seed_value = getattr(seed_cfg, field)
        nacos_value = getattr(nacos_cfg, field)
        if seed_value != nacos_value:
            diffs[field] = (seed_value, nacos_value)
    return diffs


def main(argv):
    env = os.environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        print("PUBLIC_DATA_NACOS_SERVER is required", file=sys.stderr)
        return 2
    client = build_nacos_client(
        server,
        namespace=(env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip(),
        username=(env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None,
        password=(env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None,
    )
    group = (env.get("PUBLIC_DATA_NACOS_GROUP") or "").strip() or DEFAULT_GROUP
    seed_path = argv[1] if len(argv) > 1 else DEFAULT_SEED

    seed = load_seed(seed_path)
    nacos = load_nacos(client, group)

    problems = 0
    for sid in sorted(set(seed) | set(nacos)):
        seed_cfg = seed.get(sid)
        nacos_cfg = nacos.get(sid)
        if seed_cfg is None:
            print(f"NACOS-ONLY  {sid}")
            problems += 1
            continue
        if nacos_cfg is None:
            print(f"SEED-ONLY   {sid}")
            problems += 1
            continue
        diffs = diff_fields(seed_cfg, nacos_cfg)
        if not diffs:
            continue
        problems += 1
        print(f"DIFF        {sid}")
        for field, (seed_value, nacos_value) in diffs.items():
            print(f"    {field}: seed={seed_value!r} nacos={nacos_value!r}")

    total = len(set(seed) | set(nacos))
    print(f"\n{total} entries compared, {problems} divergence(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
