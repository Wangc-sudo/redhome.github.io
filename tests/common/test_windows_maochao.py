import json
import tempfile
import unittest
from pathlib import Path

from windows_agent.maochao.agent import WindowsMaochaoAgent
from windows_agent.maochao.artifacts import file_sha256, object_key
from windows_agent.maochao.auth import (
    LOGIN_REQUIRED,
    UNKNOWN,
    VALID,
    ChromeSessionMetadata,
    LoginRequiredError,
    classify_auth_page,
)

def job_config():
    return {
        "subjects": ["1"], "restore_subject": "1",
        "page_ready_timeout_seconds": 60, "subject_attempts": 2,
        "job_lease_seconds": 900, "heartbeat_interval_seconds": 15,
        "login_wait_seconds": 1800, "auth_poll_seconds": 15,
        "agent_poll_seconds": 5,
        "notify": {"enabled": False},
        "artifacts": {"bucket": "bucket-68cc", "prefix": "price-competition"},
    }


class AuthTests(unittest.TestCase):
    def test_real_page_state_not_cookie_expiry_drives_auth(self):
        self.assertEqual(LOGIN_REQUIRED, classify_auth_page(
            "https://passport.example.test/login", "", False,
        ))
        self.assertEqual(LOGIN_REQUIRED, classify_auth_page(
            "https://web.txcs.tmall.com/", "登录状态已失效", False,
        ))
        self.assertEqual(VALID, classify_auth_page("https://web.txcs.tmall.com/", "", True))
        self.assertEqual(UNKNOWN, classify_auth_page("https://web.txcs.tmall.com/", "", False))

    def test_metadata_contains_no_cookie_and_increments_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            session = ChromeSessionMetadata(path, "profile-1")
            session.mark(LOGIN_REQUIRED)
            session.mark(VALID, recovered=True)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, data["session_version"])
            self.assertNotIn("cookie", data)
            self.assertNotIn("cookies", data)
            self.assertEqual("profile-1", data["profile_id"])


class ArtifactTests(unittest.TestCase):
    def test_object_key_is_job_scoped_and_sha_matches_file(self):
        from datetime import datetime
        key = object_key("price-competition", datetime(2026, 11, 3), "job-1", "../a.csv")
        self.assertEqual("price-competition/2026/11/03/job-1/a.csv", key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_bytes(b"abc")
            self.assertEqual(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                file_sha256(path),
            )


class _Session:
    def __init__(self):
        self.session_version = 0
        self.marks = []

    def mark(self, state, *, recovered=False):
        self.marks.append((state, recovered))
        if recovered:
            self.session_version += 1


class _Collector:
    def __init__(self, payload):
        self.payload = payload
        self.captures = 0
        self.waits = 0

    def capture(self, job_id, config, *, progress):
        self.captures += 1
        if self.captures == 1:
            raise LoginRequiredError("expired")
        from datetime import datetime, timezone
        return self.payload, [], datetime.now(timezone.utc)

    def wait_for_login_recovery(self, config, *, progress, should_stop):
        self.waits += 1


class _Client:
    def __init__(self):
        self.heartbeats = []
        self.completed = []
        self.failed = []

    def heartbeat(self, job_id, payload):
        self.heartbeats.append(payload)
        return {"action": "wait_login"}

    def complete(self, job_id, payload):
        self.completed.append((job_id, payload))
        return {"status": "completed"}

    def fail(self, job_id, payload):
        self.failed.append(payload)
        return {"status": "failed"}


class AgentRecoveryTests(unittest.TestCase):
    def test_login_recovery_restarts_same_job_and_does_not_fail(self):
        payload = {"complete": True, "rows": [], "subjects": []}
        collector = _Collector(payload)
        client = _Client()
        session = _Session()
        agent = WindowsMaochaoAgent(
            {
                "base_url": "https://example.test", "token": "secret",
                "agent_id": "windows-1", "profile_id": "profile-1", "oss": {},
            },
            client=client, collector=collector, session=session,
            uploader=lambda *args, **kwargs: [],
        )
        result = agent._run_job({
            "job_id": "job-1", "lease_token": "lease", "config": job_config(),
        })
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, collector.captures)
        self.assertEqual(1, collector.waits)
        self.assertEqual(1, session.session_version)
        self.assertEqual("job-1", client.completed[0][0])
        self.assertEqual([], client.failed)
        self.assertTrue(any(
            item["auth_state"] == LOGIN_REQUIRED for item in client.heartbeats
        ))


if __name__ == "__main__":
    unittest.main()
