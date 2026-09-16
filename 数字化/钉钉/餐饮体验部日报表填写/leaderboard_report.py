# -*- coding: utf-8 -*-
"""餐饮部门&体验部 - 销售完成率榜单（薄包装，逐群分别播报各自项目）"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import (
    build_bc_markdown, build_html, collect, iter_groups, send_group,
)

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def _cfg_for_group(g):
    """构造群视角 config：region 用群信息替换，projects 过滤本群项目。"""
    cfg = dict(CONFIG)
    cfg["region"] = {
        "name": g.get("key", ""),
        "displayName": g.get("name", ""),
        "deptOrder": g.get("projects", []),
        "broadcastExclude": g.get("broadcastExclude", []),
    }
    return cfg


def main():
    include_today = "--include-today" in sys.argv

    if "--send" in sys.argv:
        url = CONFIG["base"]["tableUrl"]
        for g in iter_groups(CONFIG):
            md = build_bc_markdown(_cfg_for_group(g), url=url, projects=g.get("projects"))
            send_group(CONFIG, g.get("robot"), "销售完成率榜", md)
        return

    if "--bc" in sys.argv:
        url = None
        args = sys.argv[sys.argv.index("--bc") + 1:]
        if args and args[0].startswith("http"):
            url = args[0]
        for g in iter_groups(CONFIG):
            print(f"===== {g.get('name')} =====")
            print(build_bc_markdown(_cfg_for_group(g), url=url, projects=g.get("projects")))
            print()
        return

    for g in iter_groups(CONFIG):
        cfg = _cfg_for_group(g)
        now, elapsed, people = collect(cfg, include_today, projects=g.get("projects"))
        html = build_html(cfg, now, elapsed, people)
        out = BASE_DIR / f"leaderboard_{g.get('key', 'group')}.html"
        out.write_text(html, encoding="utf-8")
        print(f"[{g.get('name')}] OK 已生成: {out} （参与 {len(people)} 人）")


if __name__ == "__main__":
    main()
