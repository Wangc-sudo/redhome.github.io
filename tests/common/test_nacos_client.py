"""Offline tests for the Nacos 2.x auth-aware REST client."""

import json
import unittest
import urllib.parse

from common.public_data.nacos_client import (
    NacosAuthError,
    RestNacosClient,
    build_nacos_client,
)


class FakeHttp:
    def __init__(self, *, login_payload=None, get_responses=None, post_responses=None):
        self.login_calls = []
        self.get_calls = []
        self.post_calls = []
        self.login_payload = login_payload or {"accessToken": "tok-1", "tokenTtl": 18000}
        self.get_responses = get_responses or {}
        self.post_responses = post_responses or {}

    def get(self, url, timeout):
        self.get_calls.append(url)
        return self.get_responses.get("get", (200, "content-yaml"))

    def post(self, url, form, timeout):
        if url.endswith("/nacos/v1/auth/login"):
            self.login_calls.append(form)
            return 200, json.dumps(self.login_payload)
        self.post_calls.append((url, form))
        return self.post_responses.get("post", (200, "true"))


def _client(fake, **kwargs):
    return RestNacosClient(
        "nacos:8848",
        namespace="production",
        username="nacos",
        password="secret",
        http_get=fake.get,
        http_post=fake.post,
        **kwargs,
    )


class RestNacosClientTests(unittest.TestCase):
    def test_login_then_get_config_appends_token_and_tenant(self):
        fake = FakeHttp()
        client = _client(fake)

        content = client.get_config("a.yaml", "PIPELINES")

        self.assertEqual("content-yaml", content)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(fake.get_calls[0]).query)
        self.assertEqual(["tok-1"], query["accessToken"])
        self.assertEqual(["production"], query["tenant"])
        self.assertEqual(["a.yaml"], query["dataId"])
        self.assertEqual("nacos", fake.login_calls[0]["username"])

    def test_token_is_cached_until_near_expiry(self):
        fake = FakeHttp()
        now = [1000.0]
        client = _client(fake, monotonic=lambda: now[0])

        client.get_config("a.yaml", "G")
        client.get_config("b.yaml", "G")
        self.assertEqual(1, len(fake.login_calls))

        now[0] += 18000  # beyond ttl minus margin -> re-login
        client.get_config("c.yaml", "G")
        self.assertEqual(2, len(fake.login_calls))

    def test_get_config_404_returns_none(self):
        fake = FakeHttp(get_responses={"get": (404, "")})
        client = _client(fake)

        self.assertIsNone(client.get_config("missing.yaml", "G"))

    def test_get_config_other_errors_raise(self):
        fake = FakeHttp(get_responses={"get": (500, "boom")})
        client = _client(fake)

        with self.assertRaises(NacosAuthError):
            client.get_config("a.yaml", "G")

    def test_publish_config_posts_form_with_token(self):
        fake = FakeHttp()
        client = _client(fake)

        self.assertTrue(client.publish_config("a.yaml", "G", "k: v", config_type="yaml"))

        _, form = fake.post_calls[0]
        self.assertEqual("a.yaml", form["dataId"])
        self.assertEqual("k: v", form["content"])
        self.assertEqual("yaml", form["type"])
        self.assertEqual("production", form["tenant"])
        self.assertEqual("tok-1", form["accessToken"])

    def test_publish_config_rejects_non_true_response(self):
        fake = FakeHttp(post_responses={"post": (200, "false")})
        client = _client(fake)

        with self.assertRaises(NacosAuthError):
            client.publish_config("a.yaml", "G", "x")

    def test_login_failure_raises(self):
        fake = FakeHttp()
        fake.login_payload = {}
        client = _client(fake)

        with self.assertRaises(NacosAuthError):
            client.get_config("a.yaml", "G")

    def test_public_namespace_omits_tenant(self):
        fake = FakeHttp()
        client = RestNacosClient(
            "nacos:8848", namespace="", username="u", password="p",
            http_get=fake.get, http_post=fake.post,
        )

        client.get_config("a.yaml", "G")

        query = urllib.parse.parse_qs(urllib.parse.urlparse(fake.get_calls[0]).query)
        self.assertNotIn("tenant", query)


class BuildNacosClientTests(unittest.TestCase):
    def test_authenticated_build_returns_rest_client(self):
        client = build_nacos_client("nacos:8848", namespace="ns", username="u", password="p")

        self.assertIsInstance(client, RestNacosClient)

    def test_anonymous_build_falls_back_to_sdk(self):
        client = build_nacos_client("nacos:8848", namespace="")

        self.assertNotIsInstance(client, RestNacosClient)
        self.assertTrue(hasattr(client, "get_config"))


if __name__ == "__main__":
    unittest.main()
