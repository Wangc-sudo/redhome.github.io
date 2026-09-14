"""Version-controlled annual-target seed and its ``dim_target`` replay.

Progress cards compare actuals against annual targets.  Targets are
business decisions, not extraction output, so they live in a
version-controlled seed (``docker/integration/target.seed.json``) and are
replayed wholesale into ``dim_target`` by the ``load-target`` CLI
subcommand: changing a target means editing the seed and re-running the
replay (delete-all + insert, never merge).

Error discipline: :class:`TargetSeedError` messages never contain file
contents -- only field names and row indices.
"""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.public_data.db import transaction

_TARGET_SEED_VERSION = 1
_MIN_YEAR = 2020
_MAX_YEAR = 2100

_INSERT_SQL = (
    "INSERT INTO `dim_target` "
    "(`scope`, `scope_key`, `year`, `annual_target`, `note`) "
    "VALUES (%s, %s, %s, %s, %s)"
)


class TargetSeedError(ValueError):
    """The annual-target seed is invalid (messages never contain file contents)."""


@dataclass(frozen=True)
class TargetRow:
    """One annual target for one ``(scope, scope_key, year)``."""

    scope: str
    scope_key: str
    year: int
    annual_target: Decimal
    note: str = ""


def load_target_seed(path):
    """Parse the version-controlled annual-target seed at *path*.

    Returns a tuple of :class:`TargetRow`.  ``_说明`` keys are allowed
    anywhere and ignored (seed-file convention).  Raises
    :class:`TargetSeedError` for any structural problem, including an
    empty ``targets`` list -- an empty replay would silently wipe
    ``dim_target``.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetSeedError("target seed is not a readable JSON file") from exc
    if not isinstance(raw, dict):
        raise TargetSeedError("target seed must be a JSON object")
    if raw.get("version") != _TARGET_SEED_VERSION:
        raise TargetSeedError(
            f"target seed version must be {_TARGET_SEED_VERSION}"
        )

    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise TargetSeedError("target seed targets must be a non-empty list")

    rows = []
    seen = set()
    for index, item in enumerate(targets):
        label = f"targets[{index}]"
        row = _parse_row(item, label)
        key = (row.scope, row.scope_key, row.year)
        if key in seen:
            raise TargetSeedError(
                f"target seed {label} repeats (scope, scope_key, year)"
            )
        seen.add(key)
        rows.append(row)
    return tuple(rows)


def replace_dim_target(connection, rows):
    """Replay *rows* into ``dim_target`` inside a single transaction.

    The replay is wholesale: every existing row is deleted, then *rows*
    are inserted, so the table always mirrors the seed exactly.  Values
    are bound parameters, never interpolated into the SQL text.  Returns
    the number of rows written.
    """
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute("DELETE FROM `dim_target`")
            cursor.executemany(
                _INSERT_SQL,
                [
                    (row.scope, row.scope_key, row.year,
                     row.annual_target, row.note)
                    for row in rows
                ],
            )
        return len(rows)
    finally:
        cursor.close()


def _parse_row(item, label):
    if not isinstance(item, dict):
        raise TargetSeedError(f"target seed {label} must be a JSON object")

    scope = item.get("scope")
    if not isinstance(scope, str) or not scope:
        raise TargetSeedError(f"target seed {label} needs a non-empty scope")

    scope_key = item.get("scope_key")
    if not isinstance(scope_key, str) or not scope_key:
        raise TargetSeedError(f"target seed {label} needs a non-empty scope_key")

    year = item.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        raise TargetSeedError(f"target seed {label} needs an integer year")
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        raise TargetSeedError(
            f"target seed {label}.year must be {_MIN_YEAR}..{_MAX_YEAR}"
        )

    note = item.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise TargetSeedError(f"target seed {label}.note must be a string")

    return TargetRow(
        scope=scope,
        scope_key=scope_key,
        year=year,
        annual_target=_parse_annual_target(item.get("annual_target"), label),
        note=note,
    )


def _parse_annual_target(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TargetSeedError(
            f"target seed {label} needs a numeric annual_target"
        )
    try:
        target = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TargetSeedError(
            f"target seed {label} needs a numeric annual_target"
        ) from None
    if not target.is_finite():
        raise TargetSeedError(
            f"target seed {label} needs a numeric annual_target"
        )
    if target < 0:
        raise TargetSeedError(
            f"target seed {label}.annual_target must not be negative"
        )
    return target
