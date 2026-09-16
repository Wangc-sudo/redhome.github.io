# -*- coding: utf-8 -*-
"""餐饮部门&体验部 - 表格数据体检（薄包装，逐群按项目部检查）"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import check_data, iter_groups

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    all_problems = []
    for g in iter_groups(CONFIG):
        print(f"===== {g.get('name')} =====")
        all_problems.extend(f"[{g.get('name')}] {p}"
                            for p in check_data(CONFIG, projects=g.get("projects")))
    print()
    print(f"体检完成，共 {len(all_problems)} 个问题")
