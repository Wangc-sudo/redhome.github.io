# -*- coding: utf-8 -*-
"""杭州 - 合计自动维护（薄包装）"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import recalc_totals
import json

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    recalc_totals(CONFIG)
