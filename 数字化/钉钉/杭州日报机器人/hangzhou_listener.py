# -*- coding: utf-8 -*-
"""
杭州销售日报 - 报数监听机器人（常驻）

责任人 在「线下事业部杭州日报群」发消息 @提醒事项 12345
→ 解析纯数字 → 自动写入其当日销售日报列 → 群内回复确认

格式要求：
  @提醒事项 12800      → 写入 12800
  @提醒事项 0          → 写入 0（当天无销量）
  其他格式 → 回复用法提示

前置条件（钉钉开放平台 - 应用「提醒事项」）：
  1. 开启「Stream 模式」（开发配置 - 事件与回调 - 选中 Stream 模式）
  2. 订阅事件：包含「机器人收到消息」(chatbot_message)

启动：pythonw hangzhou_listener.py  （或 python hangzhou_listener.py 前台跑）
开机自启：运行 register_autostart.bat（写入当前用户启动文件夹）
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import dingtalk_stream
from dingtalk_stream import AckMessage

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
from hangzhou_reminder import (CONFIG, DingTalk, log)

DING = CONFIG["dingtalk"]
BASE = CONFIG["base"]
ROBOT = CONFIG["robot"]
CAL = CONFIG["calendar"]

WS_RE = re.compile(r"@提醒事项\s*")  # SDK 已剥离@部分，做兜底

# 白名单缓存：uid -> 表内姓名；每 5 分钟从 config.json 重载（配合每晚组织同步自动生效）
_UID2NAME = {}
_UID2NAME_TS = 0.0
_UID2NAME_TTL = 300  # 秒


def uid2name(uid):
    """动态白名单：表格责任人(表内用名)才允许使用报数功能"""
    global _UID2NAME, _UID2NAME_TS
    import time as _t
    now = _t.time()
    if now - _UID2NAME_TS > _UID2NAME_TTL or not _UID2NAME:
        try:
            cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
            _UID2NAME = {uid_: name for name, uid_ in cfg["members"].items()
                         if not name.startswith("_")}
            _UID2NAME_TS = now
            log(f"白名单已重载: {len(_UID2NAME)} 人")
        except Exception as e:
            log(f"白名单重载失败（用旧名单）: {e}")
    return _UID2NAME.get(uid)


def day_column():
    now = datetime.now()
    if now.month != CAL["month"]:
        return None, f"当前月份 {now.month} 与配置月份 {CAL['month']} 不符"
    day = now.day
    if day in CAL["restDays"]:
        return None, None
    return f"{day}日", day


class ReportHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self):
        super().__init__()
        self._name2rid = None

    def _refresh_map(self, force=False):
        """责任人名 -> recordId 映射"""
        if self._name2rid is None or force:
            client = DingTalk(DING)
            records = client.list_records(BASE["baseId"], BASE["tableId"])
            m = {}
            for rec in records:
                fvals = rec.get("fields") or {}
                name = fvals.get("责任人")
                if name and "合计" not in str(name):
                    m[str(name).strip()] = rec["id"]
            self._name2rid = m
            log(f"记录映射已加载: {len(m)} 人")
        return self._name2rid

    def _reply(self, incoming, text):
        self.reply_text(text, incoming)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"

        text = (incoming.text.content or "").strip() if incoming.text else ""
        sender_uid = incoming.sender_staff_id or ""
        sender_name = uid2name(sender_uid)  # 动态白名单（表格责任人）

        # 门禁：非表格责任人不允许使用报数功能，实时反馈拒绝
        if not sender_name:
            log(f"门禁拦截: 非责任人(uid={sender_uid}) 尝试使用: {text[:30]}")
            try:
                self._reply(incoming,
                            "⛔ 报数功能仅限销售日报责任人使用。\n"
                            "如需填写日报请联系管理员，或在表格中直接填写。")
            except Exception:
                pass
            return AckMessage.STATUS_OK, "OK"

        try:
            self._handle_report(incoming, text, sender_uid, sender_name)
        except Exception as e:
            log(f"处理异常: {e}")
            try:
                self._reply(incoming, f"⚠️ 处理报数时出错：{e}")
            except Exception:
                pass
        return AckMessage.STATUS_OK, "OK"

    def _handle_report(self, incoming, text, sender_uid, sender_name):
        col, day = day_column()
        if col is None:
            self._reply(incoming, "今天不是销售日报工作日，无需报数～")
            return

        # 解析纯数字（允许千分位逗号/小数）
        m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)", text.replace("，", ","))
        if not m:
            self._reply(incoming,
                        f"{sender_name} 你好～报数格式：@提醒事项 数字\n"
                        f"例如：@提醒事项 12800（当天无销量报 0）")
            return
        num_str = m.group(1).replace(",", "")
        try:
            value = float(num_str)
            if value == int(value):
                value = int(value)
        except ValueError:
            self._reply(incoming, f"无法识别数字：{num_str}")
            return

        rid_map = self._refresh_map()
        rid = rid_map.get(sender_name)
        if not rid:
            self._reply(incoming, f"{sender_name}：表格中未找到你的行，请联系管理员添加")
            return

        client = DingTalk(DING)
        # 读旧值（重复报数时提示被覆盖）
        old_value = None
        try:
            records = client.list_records(BASE["baseId"], BASE["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if rec:
                old_raw = (rec.get("fields") or {}).get(col)
                if old_raw is not None and str(old_raw).strip() != "":
                    old_value = str(old_raw).replace(",", "")
        except Exception as e:
            log(f"读取旧值失败(忽略,继续覆盖写入): {e}")

        client.update_records(BASE["baseId"], BASE["tableId"],
                              [{"id": rid, "fields": {col: str(value)}}])
        log(f"报数入表: {sender_name} {col}={value} (rid={rid}, 旧值={old_value})")

        # 入表后回读该行，计算累计与完成比例
        month_total, target, ratio_str = self._calc_progress(client, rid)
        weekday = "一二三四五六日"[datetime.now().weekday()]
        lines = [
            f"✅ 已记录 {datetime.now().month}月{day}日（周{weekday}）销量：{value}",
        ]
        if old_value is not None and old_value != str(value):
            lines.append(f"🔁 已覆盖你之前填报的 {old_value}")
        if ratio_str:
            lines.append(f"📊 本月累计 {month_total} / 目标 {target}，完成 {ratio_str}")
        lines.append("祝您下班愉快 🎉")
        self._reply(incoming, "\n".join(lines))

    def _calc_progress(self, client, rid):
        """回读该行，返回 (月累计, 目标, 完成比例str)。失败返回 (None, None, None)"""
        try:
            records = client.list_records(BASE["baseId"], BASE["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if not rec:
                return None, None, None
            fvals = rec.get("fields") or {}
            # 累计 = 所有 N日 列求和（覆盖公式列可能的延迟）
            day_pat = re.compile(r"^(\d+)日$")
            total = 0.0
            for k, v in fvals.items():
                if day_pat.match(str(k)):
                    try:
                        total += float(str(v).replace(",", ""))
                    except (ValueError, TypeError):
                        pass
            total = int(total) if total == int(total) else round(total, 2)
            target = fvals.get("9月销量目标（万）") or fvals.get(f"{CAL['month']}月销量目标（万）")
            try:
                target = float(str(target).replace(",", ""))
            except (ValueError, TypeError):
                return (f"{total}", None, None)
            if target <= 0:
                return (f"{total}", f"{int(target) if target == int(target) else target}", None)
            ratio = total / target * 100
            ratio_str = f"{ratio:.1f}%"
            tgt_disp = int(target) if target == int(target) else target
            return (f"{total}", f"{tgt_disp}", ratio_str)
        except Exception as e:
            log(f"计算完成比例失败: {e}")
            return None, None, None


def main():
    credential = dingtalk_stream.Credential(DING["appKey"], DING["appSecret"])
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                                     ReportHandler())
    log("监听启动：等待 @提醒事项 报数 ...")
    client.start_forever()


if __name__ == "__main__":
    main()
