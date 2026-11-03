"""Windows 单实例 agent：claim → 采集 → ZOS → complete，登录恢复后续跑原 job。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from windows_agent.maochao.artifacts import ArtifactUploadError, upload_artifacts
from windows_agent.maochao.auth import (
    BEIJING,
    LOGIN_REQUIRED,
    VALID,
    ChromeSessionMetadata,
    LoginRequiredError,
)
from windows_agent.maochao.client import PriceAgentApiError, PriceAgentClient
from windows_agent.maochao.collector import CaptureFailedError, MaochaoCollector

logger = logging.getLogger(__name__)
AUTH_CHECK_HOURS = (6, 20)
AUTH_CHECK_WINDOW = timedelta(minutes=10)


def due_auth_check(now, last_slot=None):
    """北京时间 6:00/20:00 起十分钟内执行；同窗口不重复。"""
    local = now.astimezone(BEIJING)
    slots = [
        (local - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    ] + [
        local.replace(hour=hour, minute=0, second=0, microsecond=0)
        for hour in AUTH_CHECK_HOURS
    ]
    due = max(slot for slot in slots if slot <= local)
    if due == last_slot or local - due > AUTH_CHECK_WINDOW:
        return None
    return due


@contextmanager
def single_instance(path):
    """同一交互式 Windows 用户下只允许一个 CDP 控制者。"""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            raise RuntimeError("maochao agent is already running") from exc
        yield
    finally:
        handle.close()


class WindowsMaochaoAgent:
    def __init__(self, settings, *, client=None, collector=None, session=None, uploader=None):
        self.settings = settings
        self.client = client or PriceAgentClient(
            settings["base_url"], settings["token"], timeout=settings.get("timeout_seconds", 30),
        )
        self.collector = collector
        if self.collector is None:
            collector_options = {
                "cdp_url": settings.get("cdp_url", "http://127.0.0.1:9222"),
                "output_dir": settings.get("output_dir", r"C:\PriceAgent\data\inspection"),
            }
            if settings.get("page_url"):
                collector_options["page_url"] = settings["page_url"]
            self.collector = MaochaoCollector(**collector_options)
        self.session = session or ChromeSessionMetadata(
            settings.get("session_metadata_path", r"C:\PriceAgent\data\agent-session.json"),
            settings["profile_id"],
        )
        self.uploader = uploader or upload_artifacts
        self.agent_id = settings["agent_id"]

    def _run_job(self, claimed):
        job_id = claimed["job_id"]
        config = claimed["config"]
        lease_token = claimed["lease_token"]
        state = {
            "current_subject": "",
            "current_page": None,
            "auth_state": VALID,
        }
        stopped = threading.Event()

        def progress(**values):
            if stopped.is_set():
                raise PriceAgentApiError(409, "job_terminal")
            state.update(values)

        def heartbeat_loop():
            while not stopped.wait(config["heartbeat_interval_seconds"]):
                try:
                    result = self.client.heartbeat(job_id, {
                        "agent_id": self.agent_id,
                        "lease_token": lease_token,
                        "session_version": self.session.session_version,
                        **state,
                    })
                    if result.get("action") not in ("continue", "wait_login", "restart_job"):
                        stopped.set()
                except PriceAgentApiError as exc:
                    if exc.status in (401, 404, 409):
                        stopped.set()
                    logger.warning("price-agent heartbeat failed: %s", exc.code)
                except Exception as exc:
                    logger.warning("price-agent heartbeat failed: %s", type(exc).__name__)

        heartbeat = threading.Thread(target=heartbeat_loop, name="price-agent-heartbeat", daemon=True)
        heartbeat.start()
        try:
            while True:
                if stopped.is_set():
                    raise PriceAgentApiError(409, "job_terminal")
                try:
                    payload, local_artifacts, captured_at = self.collector.capture(
                        job_id, config, progress=progress,
                    )
                    break
                except LoginRequiredError:
                    self.session.mark(LOGIN_REQUIRED)
                    progress(auth_state=LOGIN_REQUIRED)
                    # 立即上报一次，不等下一个 15 秒心跳。
                    self.client.heartbeat(job_id, {
                        "agent_id": self.agent_id,
                        "lease_token": lease_token,
                        "session_version": self.session.session_version,
                        **state,
                    })
                    self.collector.wait_for_login_recovery(
                        config, progress=progress, should_stop=stopped.is_set,
                    )
                    self.session.mark(VALID, recovered=True)
                    progress(auth_state=VALID)
                    # 同一 job 从头重采，失效前的内存部分结果直接丢弃。
            artifacts = self.uploader(
                local_artifacts,
                config=config,
                job_id=job_id,
                captured_at=captured_at,
                oss_config=self.settings["oss"],
            )
            payload["artifacts"] = artifacts
            payload["agent_id"] = self.agent_id
            payload["lease_token"] = lease_token
            self.session.mark(VALID)
            return self.client.complete(job_id, payload)
        except PriceAgentApiError:
            raise
        except Exception as exc:
            if isinstance(exc, ArtifactUploadError):
                code = "zos_upload_failed"
            elif isinstance(exc, LoginRequiredError):
                code = "login_expired"
            elif isinstance(exc, CaptureFailedError):
                code = "capture_failed"
            else:
                code = "agent_execution_failed"
            try:
                self.client.fail(job_id, {
                    "agent_id": self.agent_id,
                    "lease_token": lease_token,
                    "error_code": code,
                    "error_detail": (
                        str(exc)[:300]
                        if isinstance(exc, (CaptureFailedError, ArtifactUploadError, LoginRequiredError))
                        else type(exc).__name__
                    ),
                })
            except Exception:
                logger.warning("price-agent fail callback failed: %s", type(exc).__name__)
            raise
        finally:
            stopped.set()
            heartbeat.join(timeout=2)

    def check_auth(self, *, checked_at):
        """独立巡检登录态；只有确认失效才上报群告警。"""
        try:
            self.collector.check_auth()
        except LoginRequiredError:
            self.session.mark(LOGIN_REQUIRED)
            return self.client.report_auth_expired({
                "agent_id": self.agent_id,
                "session_version": self.session.session_version,
                "checked_at": checked_at.isoformat(),
            })
        recovered = getattr(self.session, "auth_state", None) == LOGIN_REQUIRED
        self.session.mark(VALID, recovered=recovered)
        return {"status": VALID}

    def run_forever(self, *, stop_event=None):
        stop_event = stop_event or threading.Event()
        last_auth_check = None
        while not stop_event.is_set():
            auth_slot = due_auth_check(datetime.now(BEIJING), last_auth_check)
            if auth_slot is not None:
                try:
                    result = self.check_auth(checked_at=auth_slot)
                    last_auth_check = auth_slot
                    logger.info("price-agent scheduled auth check: %s", result["status"])
                except Exception as exc:
                    logger.warning("price-agent scheduled auth check failed: %s", type(exc).__name__)
            try:
                claimed = self.client.claim(self.agent_id)
                if claimed.get("status") == "claimed":
                    self._run_job(claimed)
                    interval = claimed["config"]["agent_poll_seconds"]
                else:
                    interval = 30 if claimed.get("status") == "disabled" else 5
            except PriceAgentApiError as exc:
                if exc.status in (401, 403):
                    logger.error("price-agent authentication rejected")
                    interval = 60
                elif exc.status == 409:
                    interval = 5
                else:
                    logger.warning("price-agent claim failed: %s", exc.code)
                    interval = 15
            except Exception as exc:
                logger.warning("price-agent job failed: %s", type(exc).__name__)
                interval = 15
            stop_event.wait(interval)


def load_settings(path=None):
    config_path = Path(path or os.environ.get("PRICE_AGENT_CONFIG", r"C:\PriceAgent\config\agent.json"))
    data = json.loads(config_path.read_text(encoding="utf-8"))
    required = ("base_url", "token", "agent_id", "profile_id", "oss")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError("missing agent settings: " + ",".join(missing))
    return data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    with single_instance(settings.get("lock_path", r"C:\PriceAgent\data\agent.lock")):
        WindowsMaochaoAgent(settings).run_forever()


if __name__ == "__main__":
    main()
