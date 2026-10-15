"""WDT 同步窗口的每日滚动（roll-manifest 管线，2026-09-23）。

背景：`source-manifest.json` 的 WDT 窗口是构建期 baked 的静态区间，
不滚动就会每天重拉同一天（幂等但无新数据）。本模块把
``scripts/build_manifest.py`` 的窗口展开逻辑上移为公共能力，由调度器
每日在 sync-wdt 之前执行：只重写 manifest 的 ``wdt.datasets`` 块
（``dingtalk`` 块原样保留），原子写避免半文件。

数据集定义仍在版本受控的 ``scripts/wdt_datasets.json``（方法白名单、
分页、id 路径、params 的单一调整点）。

2026-10-15 起另承接 L1「渠道 sheet 换月」：渠道运营每月在同 base 新建
一套 ``<渠道>渠道数据 <月>`` sheet，manifest 钉字面名会静默陈旧（10 月
零行、无报错）。传入钉钉读网关时，本模块按命名约定把渠道日销 sheet 就地
改绑到当月，换绑前用 ``validate_sheet`` 干校验，任何不确定情形一律
fail-soft 保留旧绑定 + warning（见 :func:`_roll_dingtalk_sheets`）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: WDT 数据集定义文件（相对仓库根；可用环境变量覆盖，供测试与非常规部署）。
_DEFAULT_WDT_CONFIG = Path(__file__).resolve().parents[2] / "scripts" / "wdt_datasets.json"
_WDT_CONFIG_ENV = "PUBLIC_DATA_WDT_DATASETS_CONFIG"

_DEFAULT_LOOKBACK_DAYS = 1
_DEFAULT_WINDOW_MINUTES = 50
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: 渠道 sheet 换月按北京日历月判定（AI 表 sheet 按月命名，中国无夏令时）。
_ROLL_TZ = timezone(timedelta(hours=8))

#: 命名约定（L4）``<渠道>渠道数据 <月>``的月后缀；数字前允许空白——实测
#: ``私域渠道数据 6``（带空格）与 ``私域渠道数据10`` 并存。
_MONTH_SUFFIX_RE = re.compile(r"^\s*(\d+)$")


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


def _sheet_month(sheet_name, channel):
    """*sheet_name* 属 *channel* 的 sheet 家族时返回月份数，否则 None。

    命名约定 ``<渠道>渠道数据 <月>``；``天猫渠道数据 (4)`` 这类衍生副本的
    括号后缀不匹配，天然被排除。
    """
    if not isinstance(sheet_name, str):
        return None
    prefix = f"{channel}渠道数据"
    if not sheet_name.startswith(prefix):
        return None
    match = _MONTH_SUFFIX_RE.match(sheet_name[len(prefix):])
    return int(match.group(1)) if match else None


def _channel_of(sheet):
    """渠道标识取自 sheet 的 inject transform（``values.channel``）。

    渠道日销 7 表的 transform 已注入 channel 列，直接复用这一单一真源，
    不再新增 manifest 字段；无该注入（非渠道表）返回 None —— 不入滚月范围。
    """
    transform = getattr(sheet, "transform", None)
    if not isinstance(transform, dict) or transform.get("type") != "inject":
        return None
    channel = (transform.get("values") or {}).get("channel")
    if isinstance(channel, str) and channel.strip():
        return channel.strip()
    return None


def _current_month(now=None):
    """当前北京日历月（换月以北京 1 日 00:00 为界）。"""
    moment = now or datetime.now(_ROLL_TZ)
    return moment.astimezone(_ROLL_TZ).month


def _match_current_sheet(remote_sheets, channel, month):
    """在远端 sheet 列表里找 *channel* 的当月 sheet；非唯一视为未命中。"""
    matches = []
    for remote in remote_sheets:
        if not isinstance(remote, Mapping):
            continue
        sheet_id = remote.get("id")
        if not isinstance(sheet_id, str) or not sheet_id:
            continue
        if _sheet_month(remote.get("name"), channel) == month:
            matches.append((sheet_id, remote.get("name")))
    return matches[0] if len(matches) == 1 else None


def _roll_dingtalk_sheets(manifest, parsed_sheets, gateway, *, now=None):
    """按命名约定把渠道 sheet 就地改绑到当月；返回改绑条数。

    fail-soft：未找到唯一当月 sheet / 名称歧义 / 干校验未过 / 远端列表读不到，
    一律保留旧绑定并记 warning，绝不写坏 manifest。
    """
    month = _current_month(now)
    declared_by_dataset = {sheet.dataset: sheet for sheet in parsed_sheets}
    rebound = 0
    for base in manifest.get("dingtalk", {}).get("bases", []):
        if not isinstance(base, dict):
            continue
        base_id = base.get("base_id")
        raw_sheets = base.get("sheets")
        if not isinstance(raw_sheets, list):
            continue
        remote_sheets = None
        for raw_sheet in raw_sheets:
            if not isinstance(raw_sheet, dict):
                continue
            declared = declared_by_dataset.get(raw_sheet.get("dataset"))
            channel = _channel_of(declared)
            if channel is None:
                continue
            if remote_sheets is None:
                try:
                    remote_sheets = list(gateway.list_sheets(base_id))
                except Exception:
                    logger.warning(
                        "roll-manifest: base=%s 远端 sheet 列表读取失败，"
                        "渠道 sheet 全部保留旧绑定", base_id,
                    )
                    break
            target = _match_current_sheet(remote_sheets, channel, month)
            if target is None:
                logger.warning(
                    "roll-manifest: %s 未找到唯一的当月(%d 月)sheet，保留旧绑定 %s",
                    channel, month, raw_sheet.get("sheet_name"),
                )
                continue
            sheet_id, sheet_name = target
            if (
                sheet_id == raw_sheet.get("sheet_id")
                and sheet_name == raw_sheet.get("sheet_name")
            ):
                continue
            candidate = replace(declared, sheet_id=sheet_id, sheet_name=sheet_name)
            try:
                gateway.validate_sheet(candidate)
            except Exception:
                logger.warning(
                    "roll-manifest: %s 当月 sheet %s 干校验未过，保留旧绑定 %s",
                    channel, sheet_name, raw_sheet.get("sheet_name"),
                )
                continue
            raw_sheet["sheet_id"] = sheet_id
            raw_sheet["sheet_name"] = sheet_name
            rebound += 1
    return rebound


def roll_manifest(
    manifest_path, *, lookback_days=None, config_path=None, now=None, gateway=None
) -> int:
    """就地滚动 *manifest_path* 的 WDT 窗口块；返回写入的数据集条数。

    ``dingtalk`` 块默认原样保留；传入 *gateway*（钉钉读网关）时另按命名约定
    把渠道日销 sheet 改绑到当月（L1 换月永久方案，fail-soft，见
    :func:`_roll_dingtalk_sheets`）。临时文件 + os.replace 原子写，进程中断
    不会留下半文件。
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or "dingtalk" not in manifest:
        raise ManifestRollError(f"not a source manifest: {path}")

    datasets = build_wdt_datasets(
        lookback_days=lookback_days, config_path=config_path, now=now
    )
    manifest["wdt"] = {"datasets": datasets}

    if gateway is not None:
        from common.public_data.manifest import ManifestError, load_manifest
        try:
            parsed_sheets = load_manifest(path).dingtalk_sheets
        except ManifestError:
            logger.warning("roll-manifest: manifest 结构校验未过，跳过渠道 sheet 滚月")
        else:
            _roll_dingtalk_sheets(manifest, parsed_sheets, gateway, now=now)

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
