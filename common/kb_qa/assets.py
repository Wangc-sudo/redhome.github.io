# -*- coding: utf-8 -*-
"""附件直发：AI 表附件 → 钉钉群文件/图片消息（2026-10-21）。

背景：PRODUCT_ASSET 分支此前只列附件清单（_ASSET_PENDING 占位「文件直发
功能接入中」）。media.py 的下载/上传/media_id 缓存链路早已备好，本模块
补齐最后一段——发送。

链路（全部 fail-open，任何一步失败调用方降级列清单）：
    record_id → get_record 重拉单记录（签名 URL 带时效，入库不存，用时
    现取）→ media.ensure_media_id（kb_media_cache 3 天缓存，命中毫秒级；
    未命中 下载→上传→写缓存，约 1-2s）→ send_group_message：
      图片 → sampleImageMsg {photoURL: media_id}
      文件 → sampleFile {mediaId, fileName, fileType}

附件选择精度（pick_asset）：「习酒窖藏1988 质检报告」曾被分词回退命中
红习酱的「礼袋」——商品相关度与附件字段名必须联合：先按问题里的附件
词匹配字段名（质检报告→「质检报告」字段），无匹配再回退第一个有附件
的商品（find_products 的相关度序）。
"""

from common.kb_qa import media

#: 附件意图词（字段名匹配用）。排在前的更具体，匹配时先到先得。
ASSET_TERMS = (
    "质检报告", "检测报告", "规格书", "说明书", "细节图", "素材包",
    "素材", "礼袋", "包装", "图片", "照片", "质检", "报告", "图",
)

#: 强类型附件词：明确点名证书/报告/发票类时，字段不匹配**不得**回退
#: 到别的字段（2026-10-22 事故：问红习酱质检报告被发了礼袋——证书类
#: 发错类型比如实答「没有」更糟）。弱词（图/包装/素材）保留回退。
STRONG_TERMS = (
    "质检报告", "检测报告", "规格书", "说明书", "质检", "报告",
    "发票", "证书",
)


def is_strong_asset_question(question):
    """问题是否明确点名强类型附件（证书/报告/发票类）。"""
    return any(term in (question or "") for term in STRONG_TERMS)

#: 单条消息最多直发几个附件（一个字段可能多个文件，防刷屏）。
MAX_DELIVER = 2

#: 多图字段拼长图的取图上限（2026-10-22 裁决：多图竖拼一条长图优于
#: 逐条刷屏；质检证书类实测全是 PNG 扫描件，发票字段多达 8~10 张）。
MAX_STITCH_IMAGES = 8

#: send_group_message 的 msg_key / msgParam 形态（钉钉机器人群消息）。
_IMAGE_MSG_KEY = "sampleImageMsg"
_FILE_MSG_KEY = "sampleFile"


def pick_asset(rows, question):
    """商品+附件字段联合选择，返回 (row, field_name, items)；无附件
    返回 (None, None, [])。

    优先级：字段名命中问题附件词的商品字段 > 第一个有附件的商品字段
    （find_products 的返回序即商品相关度序）。
    """
    wanted = [term for term in ASSET_TERMS if term in (question or "")]
    fallback = None
    for row in rows or []:
        attachments = row.get("attachments") or {}
        if not isinstance(attachments, dict):
            continue
        for field_name, items in attachments.items():
            if not items:
                continue
            if fallback is None:
                fallback = (row, field_name, items)
            if any(term in field_name for term in wanted):
                return row, field_name, items
    # 强类型词不回退：点名要证书而商品没有证书字段时，宁缺毋滥。
    if fallback is not None and not is_strong_asset_question(question):
        return fallback
    return (None, None, [])


class AssetSender:
    """附件直发器。所有外部调用异常上抛，由调用方（_answer）降级。"""

    def __init__(self, *, dingtalk_client, base_id, sheet_id,
                 oapi_token_factory, max_deliver=MAX_DELIVER, stitcher=None):
        self._client = dingtalk_client
        self._base_id = base_id
        self._sheet_id = sheet_id
        self._oapi_token_factory = oapi_token_factory
        self._max_deliver = max_deliver
        self._stitcher = stitcher or _default_stitcher

    def send(self, conn, *, row, field_name, conversation_id, robot_code):
        """直发 row 的 field_name 附件。返回发送的消息条数。

        形态：字段全是图片且超过逐条上限（发票/细节图多图场景）→ 竖拼
        一张长图发单条；否则逐条直发（前 max_deliver 个）。拼图任何一步
        失败回退逐条（fail-open）。
        """
        record = self._client.get_record(self._base_id, self._sheet_id,
                                         row["record_id"])
        attachments = (record.get("fields") or {}).get(field_name) or []
        if (len(attachments) > self._max_deliver
                and all(media.is_image_name(
                    m.get("filename") or m.get("name"))
                    for m in attachments)):
            try:
                return self._send_long_image(
                    conn, row=row, field_name=field_name,
                    attachments=attachments,
                    conversation_id=conversation_id, robot_code=robot_code)
            except Exception:
                pass  # 拼图失败回退逐条发送
        oapi_token = self._oapi_token_factory()
        sent = 0
        for index, meta in enumerate(attachments[:self._max_deliver]):
            media_id, media_type = media.ensure_media_id(
                conn, oapi_token=oapi_token,
                record_id=row["record_id"], field_name=field_name,
                index=index, attachment_meta=meta)
            self._deliver(media_id, media_type,
                          meta.get("filename") or meta.get("name")
                          or f"{field_name}-{index}",
                          conversation_id, robot_code)
            sent += 1
        return sent

    def _send_long_image(self, conn, *, row, field_name, attachments,
                         conversation_id, robot_code):
        """多图竖拼长图 → 上传 → 单条图片消息。media_id 走同款 3 天缓存。"""
        return self._deliver_images(
            conn, cache_key=f"{row['record_id']}:{field_name}:long",
            metas=attachments, file_name=f"{field_name}-长图.jpg",
            conversation_id=conversation_id, robot_code=robot_code)

    def _deliver_images(self, conn, *, cache_key, metas, file_name,
                        conversation_id, robot_code):
        """多图 → 长图单条发送（缓存命中直发）。返回 1。"""
        cached = media.cached_media(conn, cache_key)
        if cached:
            media_id = cached["media_id"]
        else:
            images = []
            for meta in metas[:MAX_STITCH_IMAGES]:
                url = media._extract_attachment_url(meta)
                if url:
                    images.append(media.download_bytes(url))
            content, _used = self._stitcher(images)
            media_id = media.upload_media(
                self._oapi_token_factory(), "image", file_name, content)
            media.save_media(conn, cache_key, media_id, "image", file_name)
        self._client.send_group_message(
            robot_code, conversation_id, _IMAGE_MSG_KEY,
            {"photoURL": media_id})
        return 1

    def send_image_refs(self, conn, *, refs, conversation_id, robot_code):
        """向量图召回结果直发：refs = [(record_id, field_name, index), ...]。

        回指 AI 表附件位置，get_record 现取签名 URL。单图走
        ensure_media_id 直发（命中既有单图缓存）；多图拼长图单条。
        返回发送的消息条数，0=无可发图（调用方走文本兜底）。
        """
        valid = []
        record_cache = {}
        for record_id, field_name, index in refs:
            if record_id not in record_cache:
                record_cache[record_id] = self._client.get_record(
                    self._base_id, self._sheet_id, record_id)
            items = ((record_cache[record_id].get("fields") or {})
                     .get(field_name) or [])
            if 0 <= index < len(items):
                meta = items[index]
                if media.is_image_name(meta.get("filename")
                                       or meta.get("name")):
                    valid.append((record_id, field_name, index, meta))
        if not valid:
            return 0
        if len(valid) == 1:
            record_id, field_name, index, meta = valid[0]
            media_id, _media_type = media.ensure_media_id(
                conn, oapi_token=self._oapi_token_factory(),
                record_id=record_id, field_name=field_name,
                index=index, attachment_meta=meta)
            self._deliver(media_id, "image",
                          meta.get("filename") or meta.get("name")
                          or "召回图",
                          conversation_id, robot_code)
            return 1
        # 多图长图缓存键：回指组合的指纹（附件内容变则键变，自动失效）
        import hashlib

        fingerprint = hashlib.md5(
            "|".join(f"{r}/{f}/{i}" for r, f, i, _m in valid)
            .encode()).hexdigest()[:16]
        return self._deliver_images(
            conn, cache_key=f"recall:{fingerprint}",
            metas=[m for _r, _f, _i, m in valid], file_name="召回图-长图.jpg",
            conversation_id=conversation_id, robot_code=robot_code)

    def _deliver(self, media_id, media_type, file_name,
                 conversation_id, robot_code):
        if media_type == "image":
            self._client.send_group_message(
                robot_code, conversation_id, _IMAGE_MSG_KEY,
                {"photoURL": media_id})
            return
        file_type = (file_name.rsplit(".", 1)[-1] if "." in file_name
                     else "file")
        self._client.send_group_message(
            robot_code, conversation_id, _FILE_MSG_KEY,
            {"mediaId": media_id, "fileName": file_name,
             "fileType": file_type})


def _default_stitcher(images):
    """长图拼接（Pillow 可选依赖，未安装抛 StitchError 回退逐条发送）。"""
    from common.kb_qa.long_image import stitch_vertical

    return stitch_vertical(images)


def build_from_environment(environ=None):
    """env 齐备返回 AssetSender，缺失返回 None（直发关闭，降级列清单）。

    需要 KB_DINGTALK_*（AI 表读 + 群消息发送）与 KB_PRODUCTS_BASE_ID /
    KB_PRODUCTS_SHEET_ID（记录定位）——kb-gateway 与 scheduler 同一份
    app.env，全部现成。
    """
    import os

    values = os.environ if environ is None else environ
    app_key = (values.get("KB_DINGTALK_APP_KEY") or "").strip()
    app_secret = (values.get("KB_DINGTALK_APP_SECRET") or "").strip()
    operator_id = (values.get("KB_DINGTALK_OPERATOR_ID") or "").strip()
    base_id = (values.get("KB_PRODUCTS_BASE_ID") or "").strip()
    sheet_id = (values.get("KB_PRODUCTS_SHEET_ID") or "").strip()
    if not all((app_key, app_secret, operator_id, base_id, sheet_id)):
        return None

    from common.dingtalk.client import DingTalkClient

    client = DingTalkClient(app_key, app_secret, operator_id)
    return AssetSender(
        dingtalk_client=client, base_id=base_id, sheet_id=sheet_id,
        oapi_token_factory=lambda: media.get_oapi_token(app_key, app_secret))
