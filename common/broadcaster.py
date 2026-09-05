# -*- coding: utf-8 -*-
"""
群播报器（数字中台公共层）
==========================
统一各机器人的「群 Markdown 播报 + @人 + DING 命令生成」出口。

钉钉 DING 直连 API 需要的 qyapi_create_ding 权限受限，采用替代架构：
脚本自发群消息，同时输出 DING_CMD_START...END 包裹的 dws 命令，
由定时任务 agent 原样执行完成 DING（服务端通道已授权）。

用法:
    from common.broadcaster import Broadcaster
    bc = Broadcaster(client, robot_code, open_conversation_id, log=print)
    bc.send_markdown("标题", "markdown文本", at_user_ids=[...])
    print(bc.ding_command(user_ids, "催办内容"))   # 输出给 cron agent 执行
"""
from datetime import datetime
from pathlib import Path


class Broadcaster:
    def __init__(self, client, robot_code, open_conversation_id, log=None, log_dir=None):
        """client: common.dingtalk.DingTalkClient"""
        self.client = client
        self.robot_code = robot_code
        self.conv_id = open_conversation_id
        self._log_fn = log
        self.log_dir = Path(log_dir) if log_dir else None

    # ---------- 群消息 ----------
    def send_markdown(self, title, text, at_user_ids=None):
        r = self.client.send_group_markdown(self.robot_code, self.conv_id,
                                            title, text, at_user_ids=at_user_ids)
        self.log(f"已发群消息: {title} (at={at_user_ids})")
        return r

    # ---------- DING（dws 服务端通道） ----------
    def ding_command(self, user_ids, content, ding_type="app"):
        """生成 dws DING 发送命令（由 cron agent 原样执行），返回命令字符串"""
        return (f"dws ding message send --robot-code {self.robot_code} "
                f"--users {','.join(user_ids)} --content \"{content}\" "
                f"--type {ding_type} --format json")

    def print_ding_command(self, user_ids, content, ding_type="app"):
        """按 cron agent 约定格式输出 DING 命令块"""
        if not user_ids:
            return
        print("DING_CMD_START")
        print(self.ding_command(user_ids, content, ding_type))
        print("DING_CMD_END")

    # ---------- 日志 ----------
    def log(self, msg):
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line)
        if self.log_dir:
            self.log_dir.mkdir(exist_ok=True)
            with open(self.log_dir / f"broadcast_{datetime.now():%Y%m}.log", "a",
                      encoding="utf-8") as f:
                f.write(line + "\n")
        elif self._log_fn:
            self._log_fn(msg)


def weekday_cn(d):
    """datetime → 周几（中文）"""
    return "一二三四五六日"[d.weekday()]
