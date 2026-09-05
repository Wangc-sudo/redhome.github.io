#!/usr/bin/env python3
"""
钉钉 API 客户端（渠道日报机器人用）
====================================
- 旧版 OAPI access_token 获取（webhook 发送用）
- 新版 accessToken 获取（notable / 机器人 API 用）
- AI表格（notable）：sheets 列表 / 字段列表 / 记录分页读取
- 群机器人推送：webhook 自定义机器人（支持加签）/ 企业机器人 groupMessages

调用约定沿用旧工程经验（SYNC_EXPERIENCE / sync_stock.py）：
- header: x-acs-dingtalk-access-token
- operatorId 必传
- 分页用 records + nextToken，单表约 18,000 行上限
"""
import hashlib
import hmac
import base64
import time
import urllib.parse
import urllib.request
import json


class DingTalkError(RuntimeError):
    pass


def _http_json(url, method="GET", body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class DingTalkClient:
    def __init__(self, app_key, app_secret, operator_id):
        self.app_key = app_key
        self.app_secret = app_secret
        self.operator_id = operator_id
        self._token = None
        self._token_ts = 0.0

    # ---------- token ----------
    def get_token(self, force=False):
        """新版 accessToken（api.dingtalk.com），缓存 100 分钟（有效期 2 小时）"""
        if not force and self._token and time.time() - self._token_ts < 6000:
            return self._token
        result = _http_json(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            method="POST",
            body={"appKey": self.app_key, "appSecret": self.app_secret},
        )
        if "accessToken" not in result:
            raise DingTalkError(f"获取新token失败: {result}")
        self._token = result["accessToken"]
        self._token_ts = time.time()
        return self._token

    def _headers(self):
        return {
            "x-acs-dingtalk-access-token": self.get_token(),
            "Content-Type": "application/json",
        }

    # ---------- notable / AI表格 ----------
    def list_sheets(self, base_id):
        """列出 Base 下全部数据表，返回 [{id, name, ...}]"""
        url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_id}"
               f"/sheets?operatorId={self.operator_id}")
        data = _http_json(url, headers=self._headers())
        return data.get("value", data.get("sheets", []))

    def list_fields(self, base_id, sheet_id):
        """列出表字段，返回 [{name, type, ...}]"""
        url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_id}"
               f"/sheets/{sheet_id}/fields?operatorId={self.operator_id}")
        data = _http_json(url, headers=self._headers())
        return data.get("value", data.get("fields", []))

    def list_records(self, base_id, sheet_id, page_size=100, max_pages=200):
        """分页读取全部记录。单表约 18,000 行上限，按需翻页。"""
        records, next_token, page = [], "", 0
        while page < max_pages:
            url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_id}"
                   f"/sheets/{sheet_id}/records?operatorId={self.operator_id}"
                   f"&pageSize={page_size}")
            if next_token:
                url += f"&nextToken={next_token}"
            data = _http_json(url, headers=self._headers())
            items = data.get("records", data.get("value", []))
            records.extend(items)
            page += 1
            if not data.get("hasMore") or not data.get("nextToken"):
                break
            next_token = data["nextToken"]
        return records

    # ---------- 群机器人推送 ----------
    @staticmethod
    def _webhook_url(webhook, secret=""):
        """自定义机器人加签：timestamp + "\\n" + secret 的 HmacSHA256"""
        if not secret:
            return webhook
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        hmac_code = hmac.new(
            secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in webhook else "?"
        return f"{webhook}{sep}timestamp={ts}&sign={sign}"

    def send_webhook_markdown(self, webhook, title, markdown_text, secret="", at_mobiles=None):
        """通过群自定义机器人 webhook 发送 markdown 消息"""
        url = self._webhook_url(webhook, secret)
        body = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": markdown_text},
        }
        if at_mobiles:
            body["at"] = {"atMobiles": at_mobiles}
        result = _http_json(url, method="POST", body=body)
        if result.get("errcode") != 0:
            raise DingTalkError(f"webhook 发送失败: {result}")
        return result

    def send_group_message(self, robot_code, open_conversation_id, msg_key, msg_param):
        """通过企业机器人发送群消息（需 robotCode + openConversationId）"""
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        body = {
            "robotCode": robot_code,
            "openConversationId": open_conversation_id,
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        return _http_json(url, method="POST", body=body, headers=self._headers())
