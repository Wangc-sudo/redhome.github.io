# -*- coding: utf-8 -*-
"""绍兴 - 销售完成率榜单（薄包装）"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import build_bc_markdown, collect, build_html, send_group

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def main():
    include_today = "--include-today" in sys.argv
    out = BASE_DIR / "leaderboard.html"
    if "--output" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--output") + 1])

    if "--bc" in sys.argv:
        url = None
        args = sys.argv[sys.argv.index("--bc") + 1:]
        if args and args[0].startswith("http"):
            url = args[0]
        print(build_bc_markdown(CONFIG, url=url))
        return

    if "--send" in sys.argv:
        url = CONFIG["base"]["tableUrl"]
        md = build_bc_markdown(CONFIG, url=url)
        send_group(CONFIG, "销售完成率榜", md)
        return

    now, elapsed, people = collect(CONFIG, include_today)
    html = build_html(CONFIG, now, elapsed, people)
    out.write_text(html, encoding="utf-8")
    print(f"OK 已生成: {out}")


if __name__ == "__main__":
    main()
