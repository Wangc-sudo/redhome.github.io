import hashlib
import unittest
import warnings
from contextlib import contextmanager
from unittest import mock

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.price_agent.api import register_price_agent_routes
from common.price_agent.config import normalize_config

TOKEN = "machine-secret"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


class _ConfigSource:
    def get_config(self):
        config = normalize_config({
            "subjects": ["1"], "restore_subject": "1",
            "artifacts": {"bucket": "bucket-68cc", "prefix": "price-competition"},
        })
        return config, "a" * 64


@contextmanager
def _fake_db():
    yield object()


def _app(token_hash=TOKEN_HASH):
    app = FastAPI()
    register_price_agent_routes(
        app,
        db_connector=_fake_db,
        config_source=_ConfigSource(),
        token_hash=token_hash,
    )
    return app


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


class MachineAuthTests(unittest.TestCase):
    def test_get_config_also_requires_machine_token(self):
        client = TestClient(_app())
        self.assertEqual(401, client.get("/price-agent/api/config-version").status_code)
        response = client.get("/price-agent/api/config-version", headers=_auth())
        self.assertEqual(200, response.status_code)
        self.assertEqual(["1"], response.json()["config"]["subjects"])

    def test_unconfigured_token_fails_closed(self):
        client = TestClient(_app(token_hash=None))
        self.assertEqual(
            503, client.get("/price-agent/api/config-version", headers=_auth()).status_code
        )

    def test_ops_web_anonymous_read_mode_cannot_bypass_machine_auth(self):
        from common.ops_web.app import create_app
        from tests.common.test_ops_web_app import _fake_settings
        app = create_app(
            settings=_fake_settings(), db_connector=_fake_db, auth_disabled=True,
            session_secret="test-secret",
            price_agent_config_source=_ConfigSource(), price_agent_token_hash=TOKEN_HASH,
        )
        client = TestClient(app)
        self.assertEqual(401, client.get("/price-agent/api/config-version").status_code)
        self.assertEqual(
            200, client.get("/price-agent/api/config-version", headers=_auth()).status_code
        )

    def test_wrong_token_is_rejected(self):
        client = TestClient(_app())
        response = client.get(
            "/price-agent/api/config-version", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(401, response.status_code)


class RouteTests(unittest.TestCase):
    def test_auth_expired_requires_machine_auth_and_uses_current_config(self):
        client = TestClient(_app())
        payload = {"agent_id": "windows-1", "checked_at": "2026-11-03T06:00:00+08:00"}
        self.assertEqual(401, client.post("/price-agent/api/auth-expired", json=payload).status_code)
        with mock.patch(
            "common.price_agent.api.report_auth_expired",
            return_value={"status": "login_required", "notified": True},
        ) as report:
            response = client.post("/price-agent/api/auth-expired", headers=_auth(), json=payload)
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["notified"])
        self.assertEqual(payload, report.call_args.args[2])

    def test_claim_uses_current_config_and_agent_id(self):
        client = TestClient(_app())
        with mock.patch("common.price_agent.api.claim_job", return_value={"status": "empty"}) as claim:
            response = client.post(
                "/price-agent/api/jobs/claim", headers=_auth(), json={"agent_id": "windows-1"}
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("windows-1", claim.call_args.args[1])

    def test_complete_rejects_oversized_body_before_db(self):
        client = TestClient(_app())
        response = client.post(
            "/price-agent/api/jobs/job-1/complete",
            headers={**_auth(), "Content-Length": str(3 * 1024 * 1024)},
            content=b"{}",
        )
        self.assertEqual(413, response.status_code)

    def test_heartbeat_protocol_error_is_preserved(self):
        from common.price_agent.service import PriceAgentProtocolError
        client = TestClient(_app())
        with mock.patch(
            "common.price_agent.api.heartbeat_job",
            side_effect=PriceAgentProtocolError("job_terminal", status_code=409),
        ):
            response = client.post(
                "/price-agent/api/jobs/job-1/heartbeat", headers=_auth(), json={}
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual("job_terminal", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
