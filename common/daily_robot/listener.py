# -*- coding: utf-8 -*-
import json
import re
import sys
import time as _t
from datetime import datetime
from pathlib import Path

import dingtalk_stream
from dingtalk_stream import AckMessage

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient


class ReportHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, config, log_fn=None):
        super().__init__()
        self.config = config
        self.log = log_fn or print
        self._name2rid = None
        self._uid2name = {}
        self._uid2name_ts = 0.0
        self._uid2name_ttl = 300
        self.base_dir = Path(config.get("baseDir", "."))

    def _day_column(self):
        now = datetime.now()
        cal = self.config["calendar"]
        if now.month != cal["month"]:
            return None, f"当前月份 {now.month} 与配置月份 {cal['month']} 不符"
        day = now.day
        if day in cal.get("restDays", []):
            return None, None
        return f"{day}日", day

    def _refresh_uid2name(self):
        now = _t.time()
        if now - self._uid2name_ts > self._uid2name_ttl or not self._uid2name:
            try:
                cfg = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
                self._uid2name = {uid_: name for name, uid_ in cfg["members"].items()
                                  if not name.startswith("_")}
                self._uid2name_ts = now
                self.log(f"白名单已重载: {len(self._uid2name)} 人")
            except Exception as e:
                self.log(f"白名单重载失败（用旧名单）: {e}")

    def _refresh_map(self, force=False):
        if self._name2rid is None or force:
            client = DingTalkClient.from_config(self.config["dingtalk"])
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            m = {}
            for rec in records:
                fvals = rec.get("fields") or {}
                name = fvals.get("责任人")
                if name and "合计" not in str(name):
                    m[str(name).strip()] = rec["id"]
            self._name2rid = m
            self.log(f"记录映射已加载: {len(m)} 人")

    def _reply(self, incoming, text):
        self.reply_text(text, incoming)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"

        text = (incoming.text.content or "").strip() if incoming.text else ""
        sender_uid = incoming.sender_staff_id or ""
        self._refresh_uid2name()
        sender_name = self._uid2name.get(sender_uid)

        if not sender_name:
            self.log(f"门禁拦截: 非责任人(uid={sender_uid}) 尝试使用: {text[:30]}")
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
            self.log(f"处理异常: {e}")
            try:
                self._reply(incoming, f"⚠️ 处理报数时出错：{e}")
            except Exception:
                pass
        return AckMessage.STATUS_OK, "OK"

    def _handle_report(self, incoming, text, sender_uid, sender_name):
        col, day = self._day_column()
        if col is None:
            self._reply(incoming, "今天不是销售日报工作日，无需报数～")
            return

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

        client = DingTalkClient.from_config(self.config["dingtalk"])
        old_value = None
        try:
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if rec:
                old_raw = (rec.get("fields") or {}).get(col)
                if old_raw is not None and str(old_raw).strip() != "":
                    old_value = str(old_raw).replace(",", "")
        except Exception as e:
            self.log(f"读取旧值失败(忽略,继续覆盖写入): {e}")

        client.update_records(self.config["base"]["baseId"], self.config["base"]["tableId"],
                              [{"id": rid, "fields": {col: str(value)}}])
        self.log(f"报数入表: {sender_name} {col}={value} (rid={rid}, 旧值={old_value})")

        month_total, target, ratio_str = self._calc_progress(client, rid)
        weekday = "一二三四五六日"[datetime.now().weekday()]
        lines = [f"✅ 已记录 {datetime.now().month}月{day}日（周{weekday}）销量：{value}"]
        if old_value is not None and old_value != str(value):
            lines.append(f"🔁 已覆盖你之前填报的 {old_value}")
        if ratio_str:
            lines.append(f"📊 本月累计 {month_total} / 目标 {target}，完成 {ratio_str}")
        lines.append("祝您下班愉快 🎉")
        self._reply(incoming, "\n".join(lines))

    def _calc_progress(self, client, rid):
        try:
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if not rec:
                return None, None, None
            fvals = rec.get("fields") or {}
            day_pat = re.compile(r"^(\d+)日$")
            total = 0.0
            for k, v in fvals.items():
                if day_pat.match(str(k)):
                    try:
                        total += float(str(v).replace(",", ""))
                    except (ValueError, TypeError):
                        pass
            total = int(total) if total == int(total) else round(total, 2)
            target = fvals.get("9月销量目标（万）") or fvals.get(f"{self.config['calendar']['month']}月销量目标（万）")
            try:
                target = float(str(target).replace(",", ""))
            except (ValueError, TypeError):
                return f"{total}", None, None
            if target <= 0:
                return f"{total}", f"{int(target) if target == int(target) else target}", None
            ratio = total / target * 100
            ratio_str = f"{ratio:.1f}%"
            tgt_disp = int(target) if target == int(target) else target
            return f"{total}", f"{tgt_disp}", ratio_str
        except Exception as e:
            self.log(f"计算完成比例失败: {e}")
            return None, None, None
