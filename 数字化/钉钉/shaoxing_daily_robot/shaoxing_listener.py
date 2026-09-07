# -*- coding: utf-8 -*-
"""绍兴销售日报 - 报数监听机器人（常驻）"""
import json
import sys
from datetime import datetime
from pathlib import Path

import dingtalk_stream

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import ReportHandler

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
CONFIG["baseDir"] = str(BASE_DIR)
DING = CONFIG["dingtalk"]


def log(msg):
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    credential = dingtalk_stream.Credential(DING["appKey"], DING["appSecret"])
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                                     ReportHandler(CONFIG, log_fn=log))
    log("监听启动：等待 @提醒事项 报数 ...")
    client.start_forever()


if __name__ == "__main__":
    main()
