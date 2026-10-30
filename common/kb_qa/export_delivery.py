# -*- coding: utf-8 -*-
"""小易结构化查询快照的钉钉群 XLSX 交付。"""

import os
import re

from common.kb_qa import media
from common.kb_qa.query_export import ExportLimitError, ExportService


_EXPORT_RE = re.compile(
    r"(?:导出|下载).{0,10}(?:excel|xlsx|表格|文件)|"
    r"(?:excel|xlsx).{0,6}(?:导出|下载)",
    re.IGNORECASE,
)
_EXPORT_ONLY_STOP_WORDS = (
    "上一条", "刚才", "上面", "以上", "这次", "本次", "查询", "结果",
    "数据", "表格", "导出", "下载", "excel", "xlsx", "文件", "请",
    "帮我", "麻烦", "把", "将", "一下", "一个", "成", "为", "的", "并",
)


def is_export_request(text):
    return bool(_EXPORT_RE.search(text or ""))


def is_export_only_request(text):
    """识别「把刚才结果导出 Excel」等追问，不拦截查询并导出的原问题。"""
    normalized = re.sub(r"\s+", "", (text or "").lower())
    if not is_export_request(normalized):
        return False
    for word in _EXPORT_ONLY_STOP_WORDS:
        normalized = normalized.replace(word, "")
    normalized = re.sub(r"[，。！？,.!?~～：:；;、]", "", normalized)
    return not normalized


class GroupExportSender:
    """冻结快照 → XLSX → oapi media_id → sampleFile；异常只回安全文案。"""

    def __init__(self, *, export_service, dingtalk_client, robot_code,
                 oapi_token_factory, authorizer=None, log=print):
        self._exports = export_service
        self._client = dingtalk_client
        self._robot_code = robot_code
        self._oapi_token_factory = oapi_token_factory
        self._authorizer = authorizer
        self._log = log

    def deliver_latest(self, sender_uid, conversation_id):
        if not sender_uid or not conversation_id:
            return {"sent": False, "reply": "当前会话暂不支持 Excel 文件发送。"}
        try:
            snapshot_id = self._exports.latest_snapshot_id(sender_uid, conversation_id)
            if not snapshot_id:
                return {
                    "sent": False,
                    "reply": "暂无可导出的结构化查询结果，请先完成一次数据查询。",
                }
            export_id = self._exports.create(
                snapshot_id, sender_uid, conversation_id,
                authorizer=self._authorizer,
            )
            self._exports.generate(export_id, authorizer=self._authorizer)
            path, file_name = self._exports.file_for_send(
                export_id, sender_uid, conversation_id,
                authorizer=self._authorizer,
            )
            media_id = media.upload_media(
                self._oapi_token_factory(), "file", file_name, path.read_bytes(),
            )
            response = self._client.send_group_message(
                self._robot_code,
                conversation_id,
                "sampleFile",
                {"mediaId": media_id, "fileName": file_name, "fileType": "xlsx"},
            )
            if response.get("errcode") not in (0, None):
                raise RuntimeError("DingTalk sampleFile rejected")
            self._exports.mark_sent(
                export_id, sender_uid, conversation_id,
                authorizer=self._authorizer,
            )
            status = self._exports.status(
                export_id, sender_uid, conversation_id,
                authorizer=self._authorizer,
            )
            if status.get("row_count") == 0:
                reply = "本次查询无数据，已发送带导出说明的空 Excel 模板。"
            else:
                reply = f"已生成并发送 Excel 文件，共 {status.get('row_count') or 0} 行数据。"
            return {"sent": True, "reply": reply, "export_id": export_id}
        except ExportLimitError:
            return {"sent": False, "reply": "查询结果超过导出上限，请缩小范围后重试。"}
        except PermissionError:
            return {"sent": False, "reply": "当前没有导出该结果的权限，或导出已过期。"}
        except Exception as exc:
            self._log(f"kb qa: XLSX group export failed: {type(exc).__name__}")
            return {"sent": False, "reply": "Excel 生成或发送失败，请稍后重试。"}


def build_group_export_sender(*, connection_factory, reply_client, robot_code,
                              environ=None, authorizer=None, log=print):
    """群导出独立开关，默认关闭；缺配只关闭导出，不影响问答主链路。"""
    env = os.environ if environ is None else environ
    enabled = (env.get("KB_EXPORT_GROUP_ENABLE") or "").strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return None
    app_key = (env.get("KB_DINGTALK_APP_KEY") or "").strip()
    app_secret = (env.get("KB_DINGTALK_APP_SECRET") or "").strip()
    if not (app_key and app_secret and reply_client and robot_code):
        return None
    try:
        export_service = ExportService.from_env(connection_factory, environ=env)
        if not export_service.enabled:
            return None
        return GroupExportSender(
            export_service=export_service,
            dingtalk_client=reply_client,
            robot_code=robot_code,
            oapi_token_factory=lambda: media.get_oapi_token(app_key, app_secret),
            authorizer=authorizer,
            log=log,
        )
    except Exception as exc:
        log(f"kb qa: XLSX export disabled: {type(exc).__name__}")
        return None
