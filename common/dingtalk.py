# -*- coding: utf-8 -*-
"""
钉钉开放平台统一客户端（数字中台公共层）
=========================================
合并原渠道机器人 dingtalk_client.py 与杭州机器人 hangzhou_reminder.DingTalk 两套实现。

能力:
- token: 新版 accessToken（api.dingtalk.com），缓存 100 分钟
- AI表格（notable）: 表清单 / 字段列表 / 记录分页读取 / 记录更新
- 群机器人: 企业机器人 groupMessages（markdown+@人）/ 自定义机器人 webhook（加签）

调用约定（线上验证过的经验）:
- header: x-acs-dingtalk-access-token
- operatorId 必传，且必须是 unionId（不是 userId）
- 分页用 records + nextToken，单表约 18,000 行上限
- msgKey=sampleMarkdownDX 时 msgParam 可含 atUserIds

用法:
    from common.dingtalk import DingTalkClient
    client = DingTalkClient(app_key, app_secret, operator_id)
    client.list_records(base_id, table_id)
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request


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

    @classmethod
    def from_config(cls, cfg):
        """从 config.json 的 dingtalk 段构造: {appKey, appSecret, operatorId}"""
        return cls(cfg["appKey"], cfg["appSecret"], cfg["operatorId"])

    # ---------- token ----------
    def get_token(self, force=False):
        """新版 accessToken，缓存 100 分钟（有效期 2 小时）"""
        if not force and self._token and time.time() - self._token_ts < 6000:
            return self._token
        result = _http_json(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            method="POST",
            body={"appKey": self.app_key, "appSecret": self.app_secret},
        )
        if "accessToken" not in result:
            raise DingTalkError(f"获取token失败: {result}")
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

    def update_records(self, base_id, sheet_id, updates):
        """updates: [{id: recordId, fields: {字段名: 值}}]"""
        url = (f"https://api.dingtalk.com/v1.0/notable/bases/{base_id}"
               f"/sheets/{sheet_id}/records?operatorId={self.operator_id}")
        return _http_json(url, method="PUT", body={"records": updates}, headers=self._headers())

    # ---------- 群机器人 ----------
    def send_group_markdown(self, robot_code, conv_id, title, text, at_user_ids=None):
        """企业机器人发群 Markdown（sampleMarkdownDX，支持@人）。失败抛 DingTalkError。"""
        msg_param = {"title": title, "text": text}
        if at_user_ids:
            msg_param["atUserIds"] = at_user_ids
        body = {
            "robotCode": robot_code,
            "openConversationId": conv_id,
            "msgKey": "sampleMarkdownDX",
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        r = _http_json("https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                       method="POST", body=body, headers=self._headers())
        if "errcode" in r and r["errcode"] not in (0, None):
            raise DingTalkError(f"群消息发送失败: {r}")
        return r

    def send_group_message(self, robot_code, open_conversation_id, msg_key, msg_param):
        """企业机器人发群消息（通用 msgKey 版）"""
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        body = {
            "robotCode": robot_code,
            "openConversationId": open_conversation_id,
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        return _http_json(url, method="POST", body=body, headers=self._headers())

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


def send_markdown(client, push_cfg, title, text, at_user_ids=None, at_mobiles=None):
    """按 push_cfg.mode 自动选择 webhook 或企业机器人 groupSend 发送 Markdown。

    :param client: DingTalkClient 实例
    :param push_cfg: 推送配置，需包含 mode（webhook/groupSend）及对应字段；
                     通常先经 common.test_group.resolve_target 处理以支持 TEST_MODE。
    :param title: 消息标题
    :param text: markdown 正文
    :param at_user_ids: groupSend 模式下要 @ 的用户 ID 列表
    :param at_mobiles: webhook 模式下要 @ 的手机号列表
    """
    mode = push_cfg.get("mode", "webhook")
    if mode == "webhook":
        return client.send_webhook_markdown(
            push_cfg["webhook"], title, text,
            secret=push_cfg.get("secret", ""),
            at_mobiles=at_mobiles,
        )
    if mode == "groupSend":
        return client.send_group_markdown(
            push_cfg["robotCode"], push_cfg["openConversationId"],
            title, text, at_user_ids=at_user_ids,
        )
    raise DingTalkError(f"不支持的推送模式: {mode}")
