"""运维后台**按人**登录口令（``ops_web_token``，2026-10-19 过渡期裁决）。

背景：原登录是**一枚全局共享口令** + ``user_id``（只做非空校验、不与真源
比对）——谁拿到口令就能填任意 ``user_id`` 变成任何人，包括 admin。本模块
把口令**绑定到人**：一人一枚、可吊销、库里只存哈希。

与 :mod:`ops_credential`（工号+密码）的分工：
* 本模块 = **过渡期**登录通道（免登上线前让同事自助进入），口令由 admin
  签发/吊销，用户不能自改；
* ``ops_credential`` = 长期密码体系（工号 + 可自改的密码）。

哈希选型：口令是 ``secrets.token_urlsafe(32)``（256bit 高熵），故用**单次
SHA-256** 即可——PBKDF2 慢哈希是为低熵的人工密码设计的，套在这里只会白
拖慢每次登录。校验走 ``hmac.compare_digest`` 防时序侧信道。

错误纪律：异常消息只含字段名，绝不含口令或其哈希。
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from common.public_data.db import transaction

#: 口令熵（字节）→ 256bit，token_urlsafe 后为 43 字符 URL 安全串。
TOKEN_BYTES = 32

#: 哈希算法标识（落库，便于日后平滑升级）。
ALGO = "sha256"


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_OPS_WEB_TOKEN_DDL = (
    "CREATE TABLE IF NOT EXISTS `ops_web_token` (\n"
    "  `id` INT NOT NULL AUTO_INCREMENT,\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `token_hash` VARCHAR(255) NOT NULL,\n"
    "  `algo` VARCHAR(32) NOT NULL,\n"
    "  `created_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) DEFAULT NULL,\n"
    "  `revoked_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `revoked_at` DATETIME(6) DEFAULT NULL,\n"
    "  `last_used_at` DATETIME(6) DEFAULT NULL,\n"
    "  PRIMARY KEY (`id`),\n"
    "  KEY `idx_user` (`user_id`),\n"
    "  UNIQUE KEY `uk_user_token` (`user_id`, `token_hash`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def ops_web_token_ddl_statements() -> tuple[str, ...]:
    """迁移 ``mart-ops-token-v1``：按人口令表。"""
    return (_OPS_WEB_TOKEN_DDL,)


# ---------------------------------------------------------------------------
# 纯函数（无 IO，全部可离线单测）
# ---------------------------------------------------------------------------


class TokenError(ValueError):
    """口令数据无效（消息只含字段名，不含口令/哈希）。"""


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def generate_token() -> str:
    """签发一枚新口令（256bit 熵）。明文不落库，只返回一次。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """单次 SHA-256 → hex（高熵口令无需慢哈希）。"""
    if not isinstance(token, str) or not token:
        raise TokenError("token invalid")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    """恒定时间比较；口令为空或哈希损坏 → False。"""
    try:
        actual = hash_token(token)
    except TokenError:
        return False
    if not isinstance(expected_hash, str) or not expected_hash:
        return False
    return hmac.compare_digest(actual, expected_hash)


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRow:
    """一条按人口令（``token_hash`` 只用于校验，绝不外传）。"""

    id: int
    user_id: str
    token_hash: str
    algo: str
    created_by: str | None
    created_at: str | None
    revoked_at: str | None
    last_used_at: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def fetch_active_tokens(connection, user_id):
    """该人**未吊销**的口令行列表；无 → 空列表。"""
    if not isinstance(user_id, str) or not user_id:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `id`, `user_id`, `token_hash`, `algo`, `created_by`, "
            "`created_at`, `revoked_at`, `last_used_at` "
            "FROM `ops_web_token` WHERE `user_id` = %s AND `revoked_at` IS NULL",
            (user_id,),
        )
        rows = cursor.fetchall()
    return [
        TokenRow(
            id=row["id"], user_id=row["user_id"], token_hash=row["token_hash"],
            algo=row["algo"], created_by=row["created_by"],
            created_at=row["created_at"], revoked_at=row["revoked_at"],
            last_used_at=row["last_used_at"],
        )
        for row in rows
    ]


def is_issued(connection, user_id) -> bool:
    """该人是否已签发按人口令——决定全局共享口令对其是否失效。"""
    return bool(fetch_active_tokens(connection, user_id))


def check_token(connection, user_id, token) -> bool:
    """校验该人的口令（任一枚未吊销的命中即通过）。"""
    try:
        given = hash_token(token)
    except TokenError:
        return False
    for row in fetch_active_tokens(connection, user_id):
        if row.algo == ALGO and hmac.compare_digest(row.token_hash, given):
            return True
    return False


# ---------------------------------------------------------------------------
# 写面
# ---------------------------------------------------------------------------


def issue_token(connection, user_id, *, actor) -> str:
    """为 *user_id* 签发新口令，**明文只返回一次**。

    一个人可同时持有多枚（换发期不立刻踢人），吊销是独立的行级动作。
    """
    if not isinstance(user_id, str) or not user_id:
        raise TokenError("user_id invalid")
    token = generate_token()
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `ops_web_token` (`user_id`, `token_hash`, `algo`, "
                "`created_by`, `created_at`) VALUES (%s, %s, %s, %s, %s)",
                (user_id, hash_token(token), ALGO, actor, _utc_now_text()),
            )
    return token


def revoke_tokens(connection, user_id, *, actor) -> int:
    """吊销该人**全部**未吊销口令（口令泄漏后的止血动作）。返回影响行数。"""
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `ops_web_token` SET `revoked_by` = %s, `revoked_at` = %s "
                "WHERE `user_id` = %s AND `revoked_at` IS NULL",
                (actor, _utc_now_text(), user_id),
            )
            return cursor.rowcount


def touch_used(connection, user_id, token) -> None:
    """登录成功：回写命中那枚的 ``last_used_at``（只写一次，失败不影响）。"""
    try:
        given = hash_token(token)
    except TokenError:
        return
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `ops_web_token` SET `last_used_at` = %s "
                "WHERE `user_id` = %s AND `token_hash` = %s "
                "AND `revoked_at` IS NULL",
                (_utc_now_text(), user_id, given),
            )


def list_tokens(connection):
    """admin 读：全部口令的**元数据**（永不含明文/哈希）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `id`, `user_id`, `algo`, `created_by`, `created_at`, "
            "`revoked_by`, `revoked_at`, `last_used_at` FROM `ops_web_token` "
            "ORDER BY `id` DESC")
        return [dict(row) for row in cursor.fetchall()]