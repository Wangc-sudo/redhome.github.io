"""Chrome 登录态判定与**元数据**记录；不导出、不复制原始 Cookie。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
VALID = "valid"
UNKNOWN = "unknown"
LOGIN_REQUIRED = "login_required"

_LOGIN_URL_MARKERS = ("login", "passport", "signin")
_LOGIN_TEXT_MARKERS = (
    "登录已失效", "登录状态已失效", "请重新登录", "安全验证", "请完成滑块",
    "滑动验证", "账号已退出",
)


class LoginRequiredError(RuntimeError):
    pass


def classify_auth_page(url, body_text, target_frame_found):
    """以真实页面/业务 frame 判定，不能仅看 Cookie expires。"""
    url_lower = (url or "").lower()
    text = body_text or ""
    if any(marker in url_lower for marker in _LOGIN_URL_MARKERS):
        return LOGIN_REQUIRED
    if any(marker in text for marker in _LOGIN_TEXT_MARKERS):
        return LOGIN_REQUIRED
    if target_frame_found:
        return VALID
    return UNKNOWN


class ChromeSessionMetadata:
    """只记录 Profile 引用、会话版本和检查时间，文件中没有 Cookie 值。"""

    def __init__(self, path, profile_id):
        self.path = Path(path)
        self.profile_id = profile_id
        self.session_version = 0
        self.auth_state = UNKNOWN
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if data.get("profile_id") != self.profile_id:
            return
        try:
            self.session_version = int(data.get("session_version") or 0)
        except (TypeError, ValueError):
            self.session_version = 0
        state = data.get("auth_state") or UNKNOWN
        self.auth_state = state if state in (VALID, UNKNOWN, LOGIN_REQUIRED) else UNKNOWN

    def mark(self, auth_state, *, recovered=False):
        if auth_state not in (VALID, UNKNOWN, LOGIN_REQUIRED):
            raise ValueError("invalid auth_state")
        if recovered:
            self.session_version += 1
        self.auth_state = auth_state
        payload = {
            "profile_id": self.profile_id,
            "session_version": self.session_version,
            "auth_state": auth_state,
            "auth_checked_at": datetime.now(BEIJING).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(temporary, self.path)
