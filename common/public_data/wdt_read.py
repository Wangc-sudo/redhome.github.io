"""Sealed WDT read gateway with strict allowlist, time windows, and pagination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from common.public_data.manifest import WdtDataset

ALLOWED_WDT_METHODS = frozenset(
    {
        "sales.TradeQuery.queryWithDetail",
        "wms.stockin.Purchase.queryWithDetail",
        "wms.StockSpec.search2",
        "goods_query",
    }
)

_KNOWN_ROW_KEYS = frozenset({"order", "list", "goods_list", "detail_list"})

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class WdtReadError(ValueError):
    """Raised when the WDT gateway encounters an unrecoverable problem."""


class PaginationLimitExceeded(WdtReadError):
    """Raised when a dataset requires more pages than max_pages allows."""


def _extract_nested(record: dict, path: str) -> Any:
    """Walk a dotted path into a nested dict, returning the leaf value."""
    current: Any = record
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _split_windows(
    start: datetime,
    end: datetime,
    max_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into contiguous windows of at most *max_minutes* each.

    Boundaries are placed at every multiple of *max_minutes* from *start*
    that falls within the interval (inclusive of *end*), and *end* itself
    is always appended as the final boundary.  The last window ends exactly
    at *end*.
    """
    step = timedelta(minutes=max_minutes)
    boundaries: list[datetime] = [start]
    t = start + step
    while t <= end:
        boundaries.append(t)
        t += step
    boundaries.append(end)
    return [
        (boundaries[i], boundaries[i + 1])
        for i in range(len(boundaries) - 1)
    ]


class WdtReadGateway:
    """Strict, transport-agnostic reader for WDT datasets.

    The gateway does NOT make HTTP calls.  It receives an injected *call*
    callable whose signature matches ``WdtClient.call``.
    """

    def __init__(self, call: Callable) -> None:
        self._call = call

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_dataset(self, dataset: WdtDataset) -> list[tuple[dict, str]]:
        """Read all records for *dataset*, returning ``(record, stable_id)`` pairs."""
        if dataset.method not in ALLOWED_WDT_METHODS:
            raise WdtReadError(
                f"method {dataset.method!r} is not in the WDT allowlist"
            )

        if not dataset.time_boxed:
            # Non-time-series pull (e.g. a full catalog): make a single call
            # without injecting any start/end window.
            return self._page_window(dataset, dict(dataset.params))

        all_records: list[tuple[dict, str]] = []
        windows = _split_windows(
            dataset.window_start, dataset.window_end, dataset.max_window_minutes
        )

        for win_start, win_end in windows:
            params = dict(dataset.params)
            params["start_time"] = win_start.strftime(_TIME_FMT)
            params["end_time"] = win_end.strftime(_TIME_FMT)
            all_records.extend(
                self._page_window(dataset, params)
            )

        return all_records

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _page_window(
        self,
        dataset: WdtDataset,
        params: dict[str, Any],
    ) -> list[tuple[dict, str]]:
        """Page through a single time window, returning validated records."""
        records: list[tuple[dict, str]] = []
        seen_ids: set[str] = set()

        for page_no in range(dataset.max_pages):
            response = self._call(
                dataset.method,
                params,
                page_size=dataset.page_size,
                page_no=page_no,
                calc_total=0,
            )
            rows = self._extract_rows(response, dataset)

            if not rows:
                break

            is_full_page = len(rows) == dataset.page_size
            is_last_allowed_page = page_no == dataset.max_pages - 1

            if is_full_page and is_last_allowed_page:
                raise PaginationLimitExceeded(
                    f"dataset {dataset.dataset!r} exceeded max_pages "
                    f"({dataset.max_pages}) for method {dataset.method}"
                )

            for row in rows:
                stable_id = self._extract_stable_id(row, dataset)
                if stable_id in seen_ids:
                    raise WdtReadError(
                        f"duplicate record id {stable_id!r} in dataset "
                        f"{dataset.dataset!r}"
                    )
                seen_ids.add(stable_id)
                records.append((row, stable_id))

            if not is_full_page:
                break

        return records

    @staticmethod
    def _extract_rows(response: Any, dataset: WdtDataset) -> list[dict]:
        """Extract the row list from a WDT response, validating the shape."""
        if not isinstance(response, dict):
            raise WdtReadError("WDT response is not a JSON object")

        data = response.get("data")
        if not isinstance(data, dict):
            raise WdtReadError(
                "WDT response 'data' is not a mapping – cannot locate row list"
            )

        present_keys = _KNOWN_ROW_KEYS & data.keys()
        if len(present_keys) != 1:
            raise WdtReadError(
                f"WDT response must contain exactly one known row list key "
                f"among {sorted(_KNOWN_ROW_KEYS)}; "
                f"found {sorted(present_keys)} in dataset {dataset.dataset!r}"
            )

        rows = data[present_keys.pop()]
        if not isinstance(rows, list):
            raise WdtReadError(
                "WDT response row list is not a list – cannot locate row list"
            )
        return rows

    @staticmethod
    def _extract_stable_id(row: dict, dataset: WdtDataset) -> str:
        """Extract and validate the stable record ID from a row.

        ``record_id_path`` may be a single dotted path or a comma-separated
        list of paths (e.g. ``spec_no,warehouse_no``); each part must resolve
        to a non-empty string (numeric ids such as ``rec_id`` are coerced to
        ``str``) and the parts are joined into one composite ID.
        """
        parts: list[str] = []
        for path in dataset.record_id_path.split(","):
            value = _extract_nested(row, path.strip())
            if isinstance(value, bool):
                value = None
            elif isinstance(value, int):
                value = str(value)
            if not isinstance(value, str) or not value:
                raise WdtReadError(
                    f"record_id_path {dataset.record_id_path!r} did not yield a "
                    f"non-empty string in dataset {dataset.dataset!r}"
                )
            parts.append(value)
        return "|".join(parts)
