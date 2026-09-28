"""WDT 同步窗口的每日滚动（roll-manifest 管线，2026-09-23）。

背景：`source-manifest.json` 的 WDT 窗口是构建期 baked 的静态区间，
不滚动就会每天重拉同一天（幂等但无新数据）。本模块把
``scripts/build_manifest.py`` 的窗口展开逻辑上移为公共能力，由调度器
每日在 sync-wdt 之前执行：只重写 manifest 的 ``wdt.datasets`` 块
（``dingtalk`` 块原样保留），原子写避免半文件。

数据集定义仍在版本受控的 ``scripts/wdt_datasets.json``（方法白名单、
分页、id 路径、params 的单一调整点）。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: WDT 数据集定义文件（相对仓库根；可用环境变量覆盖，供测试与非常规部署）。
_DEFAULT_WDT_CONFIG = Path(__file__).resolve().parents[2] / "scripts" / "wdt_datasets.json"
_WDT_CONFIG_ENV = "PUBLIC_DATA_WDT_DATASETS_CONFIG"

_DEFAULT_LOOKBACK_DAYS = 1
_DEFAULT_WINDOW_MINUTES = 50
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


class ManifestRollError(ValueError):
    pass


def wdt_config_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    override = (env.get(_WDT_CONFIG_ENV) or "").strip()
    return Path(override) if override else _DEFAULT_WDT_CONFIG


def load_wdt_config(config_path=None) -> dict:
    path = Path(config_path) if config_path else wdt_config_path()
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict) or not isinstance(config.get("datasets"), list):
        raise ManifestRollError(
            f"WDT config must be an object with a 'datasets' list: {path}"
        )
    return config


def build_wdt_datasets(lookback_days=None, config_path=None, *, now=None) -> list:
    """把 WDT 定义展开为具体窗口数据集。

    ``split`` 型条目按 ``window_minutes`` 切片覆盖最近 *lookback_days*
    天；``single`` 型条目（全量目录类）只给最小窗口。*now* 可注入
    （缺省取当前 UTC），窗口右边界 = now。
    """
    config = load_wdt_config(config_path)
    if lookback_days is None:
        lookback = int(config.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    else:
        lookback = int(lookback_days)
    window_minutes = int(config.get("window_minutes", _DEFAULT_WINDOW_MINUTES))
    if lookback < 1:
        raise ManifestRollError(f"lookback_days must be >= 1 (got {lookback})")

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=lookback)
    step = timedelta(minutes=window_minutes)

    datasets = []
    for entry in config["datasets"]:
        base = {
            "method": entry["method"],
            "target_table": "wdt_records",
            "record_id_path": entry["record_id_path"],
            "page_size": entry["page_size"],
            "max_pages": entry["max_pages"],
            "max_window_minutes": window_minutes,
            "params": entry.get("params", {}),
        }
        # 仅在为 False 时输出；manifest 读取方默认 True。
        if not entry.get("time_boxed", True):
            base["time_boxed"] = False

        if entry.get("window", "split") == "single":
            datasets.append({
                **base,
                "dataset": entry["dataset"],
                "window_start": start.strftime(_TIME_FMT),
                "window_end": (start + timedelta(minutes=1)).strftime(_TIME_FMT),
            })
            continue

        idx = 0
        w_start = start
        while w_start < now:
            w_end = min(w_start + step, now)
            datasets.append({
                **base,
                "dataset": f"{entry['dataset']}_{idx:04d}",
                "window_start": w_start.strftime(_TIME_FMT),
                "window_end": w_end.strftime(_TIME_FMT),
            })
            w_start = w_end
            idx += 1

    return datasets


def roll_manifest(manifest_path, *, lookback_days=None, config_path=None, now=None) -> int:
    """就地滚动 *manifest_path* 的 WDT 窗口块；返回写入的数据集条数。

    ``dingtalk`` 块与其他顶层键原样保留；临时文件 + os.replace 原子写，
    进程中断不会留下半文件。
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or "dingtalk" not in manifest:
        raise ManifestRollError(f"not a source manifest: {path}")

    datasets = build_wdt_datasets(
        lookback_days=lookback_days, config_path=config_path, now=now
    )
    manifest["wdt"] = {"datasets": datasets}

    content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    try:
        # 首选同目录临时文件 + 原子替换（crash 安全）。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        # 容器里 live 目录只读、仅 manifest 文件以 rw 单文件挂载时，
        # 同目录建临时文件被拒；落到系统临时目录再拷贝覆盖
        # （2026-09-23 dops-scheduler 实锤，写量仅 KB 级）。
        fd, tmp_name = tempfile.mkstemp(prefix="manifest-roll-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            shutil.copyfile(tmp_name, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return len(datasets)


__all__ = [
    "ManifestRollError",
    "build_wdt_datasets",
    "load_wdt_config",
    "roll_manifest",
    "wdt_config_path",
]
