"""manifest 窗口滚动（roll-manifest 管线）测试。"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from common.public_data.manifest_roll import (
    ManifestRollError,
    build_wdt_datasets,
    roll_manifest,
)

_NOW = datetime(2026, 9, 23, 0, 55, 0, tzinfo=timezone.utc)

_CONFIG = {
    "lookback_days": 1,
    "window_minutes": 50,
    "datasets": [
        {
            "dataset": "wdt_trade_detail",
            "method": "sales.TradeQuery.queryWithDetail",
            "record_id_path": "trade_no",
            "page_size": 100,
            "max_pages": 1000,
            "window": "split",
            "params": {"time_type": "2"},
        },
        {
            "dataset": "wdt_goods_catalog",
            "method": "goods.Goods.queryWithSpec",
            "record_id_path": "goods_id",
            "page_size": 100,
            "max_pages": 200,
            "window": "single",
            "params": {"hide_deleted": 1},
            "time_boxed": False,
        },
    ],
}


class BuildWdtDatasetsTests(unittest.TestCase):

    def _write_config(self, tmp):
        path = Path(tmp) / "wdt_datasets.json"
        path.write_text(json.dumps(_CONFIG, ensure_ascii=False), encoding="utf-8")
        return path

    def test_split_windows_cover_the_lookback(self):
        with tempfile.TemporaryDirectory() as tmp:
            datasets = build_wdt_datasets(config_path=self._write_config(tmp), now=_NOW)
        trade = [d for d in datasets if d["dataset"].startswith("wdt_trade_detail_")]
        self.assertEqual(trade[0]["window_start"], "2026-09-22T00:55:00Z")
        self.assertEqual(trade[-1]["window_end"], "2026-09-23T00:55:00Z")
        # 窗口首尾相接、每片 ≤ 50 分钟
        for prev, nxt in zip(trade, trade[1:]):
            self.assertEqual(prev["window_end"], nxt["window_start"])
        self.assertEqual(trade[0]["params"], {"time_type": "2"})
        self.assertNotIn("time_boxed", trade[0])

    def test_single_window_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            datasets = build_wdt_datasets(config_path=self._write_config(tmp), now=_NOW)
        catalog = [d for d in datasets if d["dataset"] == "wdt_goods_catalog"]
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["time_boxed"], False)

    def test_lookback_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            datasets = build_wdt_datasets(
                lookback_days=3, config_path=self._write_config(tmp), now=_NOW
            )
        trade = [d for d in datasets if d["dataset"].startswith("wdt_trade_detail_")]
        self.assertEqual(trade[0]["window_start"], "2026-09-20T00:55:00Z")


class RollManifestTests(unittest.TestCase):

    def test_rewrites_only_the_wdt_block_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "wdt_datasets.json"
            config_path.write_text(json.dumps(_CONFIG), encoding="utf-8")
            manifest_path = Path(tmp) / "source-manifest.json"
            manifest_path.write_text(json.dumps({
                "version": 1,
                "dingtalk": {"bases": [{"base_id": "b1", "sheets": [{"sheet_id": "s1"}]}]},
                "wdt": {"datasets": [{"dataset": "stale_window"}]},
            }), encoding="utf-8")

            written = roll_manifest(
                manifest_path, config_path=config_path, now=_NOW
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(written, len(manifest["wdt"]["datasets"]))
            # dingtalk 块原样保留
            self.assertEqual(
                manifest["dingtalk"]["bases"][0]["sheets"][0]["sheet_id"], "s1"
            )
            self.assertNotIn("stale_window", {
                d["dataset"] for d in manifest["wdt"]["datasets"]
            })
            # 目录无临时文件残留
            self.assertEqual(
                [p.name for p in Path(tmp).glob("*.tmp")], []
            )

    def test_rejects_non_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ManifestRollError):
                roll_manifest(path, now=_NOW)

    def test_read_only_dir_falls_back_to_copy_over(self):
        """同目录不可写（容器 ro 挂载 + 单文件 rw）时回退拷贝覆盖。"""
        import common.public_data.manifest_roll as roll_mod

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "wdt_datasets.json"
            config_path.write_text(json.dumps(_CONFIG), encoding="utf-8")
            manifest_path = Path(tmp) / "source-manifest.json"
            manifest_path.write_text(json.dumps({
                "version": 1,
                "dingtalk": {"bases": []},
                "wdt": {"datasets": []},
            }), encoding="utf-8")

            real_mkstemp = tempfile.mkstemp
            calls = {"n": 0}

            def flaky_mkstemp(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1 and kwargs.get("dir"):
                    raise OSError("read-only file system")
                return real_mkstemp(*args, **kwargs)

            with patch.object(roll_mod.tempfile, "mkstemp", flaky_mkstemp):
                written = roll_manifest(
                    manifest_path, config_path=config_path, now=_NOW
                )

            self.assertGreater(written, 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["wdt"]["datasets"])
            self.assertEqual(manifest["dingtalk"], {"bases": []})
            self.assertEqual(calls["n"], 2)  # 第一次被拒后走了回退


class RollManifestCliTests(unittest.TestCase):

    def test_handler_rolls_the_configured_manifest(self):
        from common.daily_robot import mart_cli  # noqa: F401  (确保包可导入)
        from common.public_data import cli

        with tempfile.TemporaryDirectory() as tmp, \
             patch("common.public_data.cli.load_settings") as load_settings, \
             patch("common.public_data.cli.resolve_service_id", return_value="roll-manifest"), \
             patch("common.public_data.cli._pipeline_enabled", return_value=True), \
             patch("common.public_data.cli.roll_manifest_windows", return_value=145) as roll:
            from pathlib import Path as _P
            load_settings.return_value = Mock_settings(_P(tmp) / "source-manifest.json")
            from io import StringIO
            from contextlib import redirect_stdout
            output = StringIO()
            with redirect_stdout(output):
                cli.main(["roll-manifest", "--confirm-local-test-write"])
        roll.assert_called_once()
        self.assertIn("datasets=145", output.getvalue())
        self.assertIn("status=completed", output.getvalue())

    def test_handler_requires_confirmation(self):
        from common.public_data import cli
        with self.assertRaises(SystemExit) as raised:
            cli.main(["roll-manifest"])
        self.assertNotEqual(0, raised.exception.code)


class Mock_settings:
    def __init__(self, path):
        self.source_config_path = path


if __name__ == "__main__":
    unittest.main()
