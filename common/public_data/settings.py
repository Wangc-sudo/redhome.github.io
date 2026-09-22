import json
import os
from dataclasses import dataclass
from pathlib import Path

from common.public_data.deploy_targets import target_profile


_REQUIRED_ENVIRONMENT_KEYS = (
    "APP_ENV",
    "PUBLIC_DATA_RDS_HOST",
    "PUBLIC_DATA_RDS_PORT",
    "PUBLIC_DATA_RDS_USER",
    "PUBLIC_DATA_RDS_PASSWORD",
    "PUBLIC_DATA_DINGTALK_DATABASE",
    "PUBLIC_DATA_WDT_DATABASE",
    "PUBLIC_DATA_MART_DATABASE",
    "PUBLIC_DATA_CONFIG",
)


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    user: str
    password: str
    name: str


@dataclass(frozen=True)
class Settings:
    app_env: str
    dingtalk_database: DatabaseSettings
    wdt_database: DatabaseSettings
    mart_database: DatabaseSettings
    source_config_path: Path
    #: 人工报表导入通道的 raw 库（C 类数据源）。可选环境变量
    #: ``PUBLIC_DATA_MANUAL_DATABASE``；不配时按
    #: ``raw_manual`` + 环境后缀推导，既有部署无需改配置即可建表。
    manual_database: DatabaseSettings | None = None
    #: Optional version-controlled workday-calendar seed consumed by the
    #: extraction layer.  Unset means "skip ``dim_calendar``" rather than fail:
    #: the fact projections stay valid without it.
    calendar_seed_path: Path | None = None
    #: Optional version-controlled "region -> top-level dept ids" seed
    #: consumed by ``sync-dingtalk`` when the manifest declares the contact
    #: directory.  Unset + declared = loud failure at sync time.
    org_seed_path: Path | None = None
    #: Optional version-controlled business-region seed consumed by the
    #: ``robot`` / ``dingtalk-gateway`` containers (stage 4).  Unset means
    #: those commands cannot resolve any region config and fail loudly.
    region_seed_path: Path | None = None
    #: mart 拆库三个新 schema（设计稿 2026-09-16 §2.2）。均为可选环境变量
    #: （``PUBLIC_DATA_MART_FACTS_DATABASE`` / ``..._DIMS_...`` /
    #: ``..._QUEUE_...``）；不配时字段为 ``None``，由
    #: ``mart_routing.resolve_*`` 回落 ``mart_database``——灰度期零配置
    #: 即旧行为。配了就必须过 ``*_test`` 铁律（并入 database_names 校验）。
    mart_facts_database: DatabaseSettings | None = None
    mart_dims_database: DatabaseSettings | None = None
    mart_queue_database: DatabaseSettings | None = None

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        values = {key: environment.get(key) for key in _REQUIRED_ENVIRONMENT_KEYS}
        missing_keys = [
            key
            for key, value in values.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if missing_keys:
            raise ValueError(
                "Missing required public-data settings: " + ", ".join(missing_keys)
            )

        app_env = values["APP_ENV"]
        if app_env not in ("test", "production"):
            raise ValueError("APP_ENV must be either 'test' or 'production'")

        port_value = values["PUBLIC_DATA_RDS_PORT"]
        if not port_value.isdecimal():
            raise ValueError("PUBLIC_DATA_RDS_PORT must be a decimal integer")
        port = int(port_value)
        if not 1 <= port <= 65535:
            raise ValueError("PUBLIC_DATA_RDS_PORT must be between 1 and 65535")

        database_names = {
            "dingtalk": values["PUBLIC_DATA_DINGTALK_DATABASE"],
            "wdt": values["PUBLIC_DATA_WDT_DATABASE"],
            "mart": values["PUBLIC_DATA_MART_DATABASE"],
        }
        # 人工报表库可选：不配时按环境推导，保证「测试环境强制 *_test」
        # 这条铁律对新增库同样成立。
        manual_name = (environment.get("PUBLIC_DATA_MANUAL_DATABASE") or "").strip()
        if manual_name:
            database_names["manual"] = manual_name
        else:
            database_names["manual"] = (
                "raw_manual_test" if app_env == "test" else "raw_manual"
            )

        # mart 拆库新 schema：可选；不配时不进 database_names（字段留 None，
        # 路由层回落旧库），配了就与既有库同名同则受 *_test 铁律约束。
        for key, env_var in (
            ("mart_facts", "PUBLIC_DATA_MART_FACTS_DATABASE"),
            ("mart_dims", "PUBLIC_DATA_MART_DIMS_DATABASE"),
            ("mart_queue", "PUBLIC_DATA_MART_QUEUE_DATABASE"),
        ):
            split_name = (environment.get(env_var) or "").strip()
            if split_name:
                database_names[key] = split_name

        # 库名后缀铁律改为查 deploy_targets 同一张 profile 表（方案 B），
        # 同时校验显式 PUBLIC_DATA_TARGET 与 APP_ENV 的一致性。
        _, target = target_profile(app_env, environment)
        suffix = target["database_suffix"]
        for database_name in database_names.values():
            if suffix is not None and not database_name.endswith(suffix):
                raise ValueError(
                    f"APP_ENV={app_env} requires database names ending in '{suffix}'"
                )
            if suffix is None and database_name.endswith("_test"):
                raise ValueError(
                    f"APP_ENV={app_env} rejects database names ending in '_test'"
                )

        config_path = Path(values["PUBLIC_DATA_CONFIG"])
        try:
            with config_path.open(encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("PUBLIC_DATA_CONFIG must contain a JSON object") from error
        if not isinstance(config, dict):
            raise ValueError("PUBLIC_DATA_CONFIG must contain a JSON object")

        connection_values = {
            "host": values["PUBLIC_DATA_RDS_HOST"],
            "port": port,
            "user": values["PUBLIC_DATA_RDS_USER"],
            "password": values["PUBLIC_DATA_RDS_PASSWORD"],
        }

        calendar_seed_path = None
        calendar_seed_value = (environment.get("PUBLIC_DATA_CALENDAR_SEED") or "").strip()
        if calendar_seed_value:
            calendar_seed_path = Path(calendar_seed_value)
            if not calendar_seed_path.is_file():
                raise ValueError(
                    "PUBLIC_DATA_CALENDAR_SEED must point to an existing file"
                )

        org_seed_path = None
        org_seed_value = (environment.get("PUBLIC_DATA_ORG_SEED") or "").strip()
        if org_seed_value:
            org_seed_path = Path(org_seed_value)
            if not org_seed_path.is_file():
                raise ValueError(
                    "PUBLIC_DATA_ORG_SEED must point to an existing file"
                )

        region_seed_path = None
        region_seed_value = (environment.get("PUBLIC_DATA_REGION_SEED") or "").strip()
        if region_seed_value:
            region_seed_path = Path(region_seed_value)
            if not region_seed_path.is_file():
                raise ValueError(
                    "PUBLIC_DATA_REGION_SEED must point to an existing file"
                )

        return cls(
            app_env=app_env,
            dingtalk_database=DatabaseSettings(
                name=database_names["dingtalk"], **connection_values
            ),
            wdt_database=DatabaseSettings(name=database_names["wdt"], **connection_values),
            mart_database=DatabaseSettings(name=database_names["mart"], **connection_values),
            manual_database=DatabaseSettings(
                name=database_names["manual"], **connection_values
            ),
            source_config_path=config_path,
            calendar_seed_path=calendar_seed_path,
            org_seed_path=org_seed_path,
            region_seed_path=region_seed_path,
            mart_facts_database=(
                DatabaseSettings(name=database_names["mart_facts"], **connection_values)
                if "mart_facts" in database_names
                else None
            ),
            mart_dims_database=(
                DatabaseSettings(name=database_names["mart_dims"], **connection_values)
                if "mart_dims" in database_names
                else None
            ),
            mart_queue_database=(
                DatabaseSettings(name=database_names["mart_queue"], **connection_values)
                if "mart_queue" in database_names
                else None
            ),
        )
