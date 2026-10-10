# -*- coding: utf-8 -*-
"""附件处理：AI 表附件 → 下载 → 钉钉媒体库 → media_id 缓存。

设计（2026-10-10 定稿）：
- kb_products.attachments 只存元信息（名称/大小/类型），不存 URL——
  AI 表附件返回的是带时效签名 URL，必须用时现取现下；
- media_id 3 天有效，kb_media_cache 命中未过期直发（毫秒级），
  过期重走下载+上传（约 1-2s，仍在 5s 预算内）；
- 图片走 image 消息、PDF/zip 走 file 消息，超限退回链接兜底。

注意：附件字段返回结构的 URL 键名待首跑实测校准（记录 API 实测后
修订 _extract_attachment_url）。媒体上传走 oapi 旧版 token 体系
（gettoken），与 api.dingtalk.com 新版 token 不通用。
"""

import json
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

#: media_id 缓存时长：官方 3 天，提前 1 小时作废防边界。
MEDIA_TTL = timedelta(days=3, hours=-1)

_OAPI_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
_OAPI_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def is_image_name(file_name):
    return (file_name or "").lower().endswith(_IMAGE_EXTS)


def media_type_for(file_name):
    return "image" if is_image_name(file_name) else "file"


def cache_key_for(record_id, field_name, index):
    return f"{record_id}:{field_name}:{index}"


# ---------------------------------------------------------------------------
# 缓存读写（连接注入，测试无需真实 DB）
# ---------------------------------------------------------------------------

def cached_media(conn, cache_key, *, now=None):
    """查未过期的 media_id 缓存，未命中/已过期返回 None。"""
    now = now or datetime.now()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT `media_id`, `media_type`, `file_name` FROM `kb_media_cache`"
            " WHERE `cache_key`=%s AND `expires_at`>%s",
            (cache_key, now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def save_media(conn, cache_key, media_id, media_type, file_name, *, now=None):
    """写 media_id 缓存（同键覆盖）。"""
    now = now or datetime.now()
    expires = now + MEDIA_TTL
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO `kb_media_cache`"
            " (`cache_key`, `media_id`, `media_type`, `file_name`,"
            "  `expires_at`, `created_at`) VALUES (%s,%s,%s,%s,%s,%s) AS new"
            " ON DUPLICATE KEY UPDATE"
            " `media_id`=new.`media_id`, `media_type`=new.`media_type`,"
            " `file_name`=new.`file_name`, `expires_at`=new.`expires_at`",
            (
                cache_key, media_id, media_type, file_name,
                expires.strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 钉钉媒体上传（oapi 旧版 token；multipart 手工编码，零第三方依赖）
# ---------------------------------------------------------------------------

def get_oapi_token(app_key, app_secret, *, timeout=15):
    """oapi 旧版 access_token（media/upload 只认这个体系）。"""
    query = urllib.parse.urlencode({"appkey": app_key, "appsecret": app_secret})
    with urllib.request.urlopen(f"{_OAPI_TOKEN_URL}?{query}", timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    if result.get("errcode") != 0:
        raise RuntimeError("oapi token 获取失败")
    return result["access_token"]


def upload_media(oapi_token, media_type, file_name, content, *, timeout=60):
    """上传字节流到钉钉媒体库，返回 media_id。"""
    boundary = f"----kbqa{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{file_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    query = urllib.parse.urlencode(
        {"access_token": oapi_token, "type": media_type}
    )
    request = urllib.request.Request(
        f"{_OAPI_UPLOAD_URL}?{query}",
        data=head + content + tail,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    if result.get("errcode") != 0 or not result.get("media_id"):
        raise RuntimeError("media upload 失败")
    return result["media_id"]


def download_bytes(url, *, timeout=60):
    """按签名 URL 下载附件字节流。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _extract_attachment_url(attachment_meta):
    """从附件元信息取下载 URL。

    2026-10-10 实测附件结构含 ``url``（alidocs OSS 绝对地址）；入库的
    attachments JSON 不存 URL（会过期），用的时候重拉记录现取。
    """
    if not isinstance(attachment_meta, dict):
        return None
    for key in ("url", "downloadUrl", "download_url", "tmpUrl"):
        value = attachment_meta.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def ensure_media_id(conn, *, oapi_token, record_id, field_name, index,
                    attachment_meta, now=None):
    """缓存命中直返；未命中走 下载→上传→写缓存。返回 (media_id, media_type)。"""
    cache_key = cache_key_for(record_id, field_name, index)
    cached = cached_media(conn, cache_key, now=now)
    if cached:
        return cached["media_id"], cached["media_type"]
    url = _extract_attachment_url(attachment_meta)
    if not url:
        raise RuntimeError("附件无可用下载地址")
    file_name = attachment_meta.get("name") or f"{field_name}-{index}"
    content = download_bytes(url)
    media_type = media_type_for(file_name)
    media_id = upload_media(oapi_token, media_type, file_name, content)
    save_media(conn, cache_key, media_id, media_type, file_name, now=now)
    return media_id, media_type
