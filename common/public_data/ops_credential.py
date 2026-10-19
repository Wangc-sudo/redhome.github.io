"""运维后台登录凭据（``ops_web_credential``，2026-10-19 用户裁决）。

裁决要点：用户名 = 工号（``dim_robot_member.job_number``，标识符可公开），
密码 = **系统生成的随机初始密码**（不是手机号/工号/钉钉号——那些是组织
属性，不可轮换、可枚举，不能当密码）。

设计纪律：

* **不引新依赖**——密码哈希用 stdlib ``hashlib.pbkdf2_hmac``（SHA-256 +
  每用户随机盐 + 600k 迭代），与 :mod:`common.bi_web.auth` 的「纯 stdlib
  session」同一纪律。校验走 ``hmac.compare_digest`` 防时序侧信道。
* **签发会话 ≠ 获得权限**——本模块只回答「口令对不对」，权限仍由
  ``bi_authz`` 逐请求 fail-closed 判定（既有铁律，不因登录方式改变）。
* **防探测**——工号不存在 / 密码错误 / 已锁定，对外一律同一形态失败，
  只在「凭据存在」时才付出哈希算力（不存在时仍跑一次等价哈希，抹平时序
  差异）。
* **限流与锁定**——连续失败累计到阈值锁定一段时间，成功即清零。
* **初始密码只显示一次**——库里只存哈希，明文不在库、不进日志。

错误纪律：异常消息只含字段名，绝不含密码、哈希或盐。
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from common.public_data.db import transaction

#: 哈希算法标识（落库，便于日后平滑升级迭代数/算法）。
ALGO = "pbkdf2-sha256"

#: PBKDF2 迭代次数（OWASP 2023 对 SHA-256 的建议量级）。
ITERATIONS = 600_000

#: 盐长度（字节）。
SALT_BYTES = 16

#: 工号形态：字母数字与常见分隔符，3-32 字符（通讯录实测形如 HY-006）。
JOB_NUMBER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

#: 密码复杂度下限（初始密码由系统生成，必满足；自设密码按此校验）。
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

#: 连续失败锁定阈值与锁定时长。
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15

#: 生成初始密码用的易读字母表（去掉 0/O/1/l/I 等易混字符）。
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"
_INITIAL_PASSWORD_LENGTH = 14


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_OPS_WEB_CREDENTIAL_DDL = (
    "CREATE TABLE IF NOT EXISTS `ops_web_credential` (\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `algo` VARCHAR(32) NOT NULL,\n"
    "  `iterations` INT NOT NULL,\n"
    "  `salt_hex` VARCHAR(64) NOT NULL,\n"
    "  `password_hash` VARCHAR(255) NOT NULL,\n"
    "  `must_change` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `failed_count` INT NOT NULL DEFAULT 0,\n"
    "  `locked_until` DATETIME(6) DEFAULT NULL,\n"
    "  `created_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  PRIMARY KEY (`user_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def ops_web_credential_ddl_statements() -> tuple[str, ...]:
    """迁移 ``mart-ops-credential-v1``：凭据表。"""
    return (_OPS_WEB_CREDENTIAL_DDL,)


# ---------------------------------------------------------------------------
# 纯函数（无 IO，全部可离线单测）
# ---------------------------------------------------------------------------


class CredentialError(ValueError):
    """凭据数据无效（消息只含字段名，不含密码/哈希/盐）。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_text() -> str:
    return _utc_now().strftime("%Y-%m-%d %H:%M:%S.%f")


def generate_password() -> str:
    """系统生成的随机初始密码（易读字母表，长度 14）。明文不落库。"""
    return "".join(
        secrets.choice(_PASSWORD_ALPHABET)
        for _ in range(_INITIAL_PASSWORD_LENGTH)
    )


def hash_password(password: str, *, salt_hex: str, iterations: int = ITERATIONS) -> str:
    """PBKDF2-SHA256 → hex。纯函数，同样输入恒定输出。"""
    if not isinstance(password, str) or not password:
        raise CredentialError("password invalid")
    if not isinstance(salt_hex, str) or not salt_hex:
        raise CredentialError("salt invalid")
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return derived.hex()


def new_salt_hex() -> str:
    return secrets.token_hex(SALT_BYTES)


def verify_password(password: str, *, salt_hex: str, iterations: int,
                    expected_hash: str) -> bool:
    """恒定时间比较。任一字段损坏 → False（不抛，避免分支外泄）。"""
    try:
        actual = hash_password(password, salt_hex=salt_hex, iterations=iterations)
    except CredentialError:
        return False
    return hmac.compare_digest(actual, expected_hash)


def validate_password(password) -> str:
    """校验自设密码复杂度；不合规抛 :class:`CredentialError`。"""
    if not isinstance(password, str):
        raise CredentialError("password invalid")
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise CredentialError("password length invalid")
    if password.strip() != password or not password:
        raise CredentialError("password invalid")
    return password


def validate_job_number(job_number) -> str:
    """校验工号形态（防把任意 SQL/超长串送进查询）。"""
    if not isinstance(job_number, str):
        raise CredentialError("job_number invalid")
    value = job_number.strip()
    if not JOB_NUMBER_RE.fullmatch(value):
        raise CredentialError("job_number invalid")
    return value


def is_locked(locked_until, now: datetime | None = None) -> bool:
    """锁定窗口判定：``locked_until`` 为空或已过 → 未锁定。"""
    if locked_until is None:
        return False
    if isinstance(locked_until, str):
        try:
            locked_until = datetime.fromisoformat(locked_until)
        except ValueError:
            return False
    current = now or _utc_now()
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > current


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialRow:
    """一条凭据（``password_hash`` 只用于校验，绝不外传）。"""

    user_id: str
    algo: str
    iterations: int
    salt_hex: str
    password_hash: str
    must_change: bool
    failed_count: int
    locked_until: str | None


def fetch_credential(connection, user_id) -> CredentialRow | None:
    """按 ``user_id`` 读凭据；无记录 → ``None``。"""
    if not isinstance(user_id, str) or not user_id:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `user_id`, `algo`, `iterations`, `salt_hex`, "
            "`password_hash`, `must_change`, `failed_count`, `locked_until` "
            "FROM `ops_web_credential` WHERE `user_id` = %s",
            (user_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return CredentialRow(
        user_id=row["user_id"],
        algo=row["algo"],
        iterations=int(row["iterations"]),
        salt_hex=row["salt_hex"],
        password_hash=row["password_hash"],
        must_change=bool(row["must_change"]),
        failed_count=int(row["failed_count"]),
        locked_until=row["locked_until"],
    )


def resolve_user_by_job_number(connection, job_number):
    """工号 → ``(user_id, name)``；无此人/工号为空 → ``None``。

    只认**在职**成员（``is_active = 1``）：离职即失效，与
    ``bi_authz`` 的在职语义保持一致。
    """
    value = validate_job_number(job_number)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `user_id`, `name` FROM `dim_robot_member` "
            "WHERE `job_number` = %s AND `is_active` = 1 "
            "ORDER BY `user_id` LIMIT 2",
            (value,),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # 工号重复 = 通讯录数据问题，宁可拒绝登录也不猜人。
        return None
    return rows[0]["user_id"], rows[0]["name"]


# ---------------------------------------------------------------------------
# 写面（全部同事务，逐条审计由调用方落在自己的审计表）
# ---------------------------------------------------------------------------


def _write_credential(connection, *, user_id, salt_hex, password_hash, actor,
                      must_change, created_by=None):
    now = _utc_now_text()
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `ops_web_credential` "
                "(`user_id`, `algo`, `iterations`, `salt_hex`, `password_hash`, "
                " `must_change`, `failed_count`, `locked_until`, "
                " `created_by`, `updated_by`, `created_at`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, 0, NULL, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  `algo` = VALUES(`algo`), "
                "  `iterations` = VALUES(`iterations`), "
                "  `salt_hex` = VALUES(`salt_hex`), "
                "  `password_hash` = VALUES(`password_hash`), "
                "  `must_change` = VALUES(`must_change`), "
                "  `failed_count` = 0, "
                "  `locked_until` = NULL, "
                "  `updated_by` = VALUES(`updated_by`), "
                "  `updated_at` = VALUES(`updated_at`)",
                (user_id, ALGO, ITERATIONS, salt_hex, password_hash,
                 1 if must_change else 0,
                 created_by if created_by is not None else actor,
                 actor, now, now),
            )


def issue_initial_password(connection, user_id, *, actor) -> str:
    """为 *user_id* 生成随机初始密码并落库，**明文只返回一次**。

    置 ``must_change=1``（首登强制改密）、清零失败计数与锁定。
    """
    if not isinstance(user_id, str) or not user_id:
        raise CredentialError("user_id invalid")
    password = generate_password()
    salt_hex = new_salt_hex()
    _write_credential(
        connection,
        user_id=user_id,
        salt_hex=salt_hex,
        password_hash=hash_password(password, salt_hex=salt_hex),
        actor=actor,
        must_change=True,
    )
    return password


def set_password(connection, user_id, password, *, actor, must_change=False):
    """设置/重置密码（自改或管理员重置）。"""
    value = validate_password(password)
    salt_hex = new_salt_hex()          # 盐只生成一次，哈希必须用同一枚
    _write_credential(
        connection,
        user_id=user_id,
        salt_hex=salt_hex,
        password_hash=hash_password(value, salt_hex=salt_hex),
        actor=actor,
        must_change=must_change,
    )
    return 1


def mark_must_change(connection, user_id, *, must_change, actor) -> int:
    """翻转首登强制改密标记（用户改密后调用）。"""
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `ops_web_credential` SET `must_change` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `user_id` = %s",
                (1 if must_change else 0, actor, _utc_now_text(), user_id),
            )
            return cursor.rowcount


def register_failure(connection, user_id, *, now: datetime | None = None) -> None:
    """记一次失败：计数 +1，达阈值即锁定。凭据不存在时无操作。"""
    current = now or _utc_now()
    locked_until = None
    row = fetch_credential(connection, user_id)
    if row is None:
        return
    failed = row.failed_count + 1
    if failed >= MAX_FAILED_ATTEMPTS:
        locked_until = (current + timedelta(minutes=LOCK_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `ops_web_credential` SET `failed_count` = %s, "
                "`locked_until` = %s WHERE `user_id` = %s",
                (failed, locked_until, user_id),
            )


def clear_failures(connection, user_id) -> None:
    """登录成功：清零失败计数与锁定。"""
    with connection.cursor() as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `ops_web_credential` SET `failed_count` = 0, "
                "`locked_until` = NULL WHERE `user_id` = %s",
                (user_id,),
            )