"""IPv4 出网基线与 IPv6 卡死门禁测试（2026-09-22，T7 冒烟断点）。

全部使用注入的假 getaddrinfo/connect，零外网、零 DNS 依赖。
"""

import socket
import unittest
from types import SimpleNamespace

from common.public_data.ipv4_egress import (
    ENV_ALLOW_IPV6,
    allow_ipv6,
    apply_ipv4_baseline,
    force_ipv4,
    ipv6_egress_stalled,
)
from common.public_data.live_safety import (
    LiveRunRejected,
    require_gateway_run,
    require_ipv4_egress,
    require_live_run,
)

PROD_NAMES = ("raw_dingtalk", "raw_wdt", "mart_ops")

_V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))
_V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2401::1", 443, 0, 0))


def _resolver(results=None, error=None):
    def getaddrinfo(host, port, **kwargs):
        if error is not None:
            raise error
        return list(results)

    return getaddrinfo


def _settings():
    return SimpleNamespace(
        app_env="production",
        dingtalk_database=SimpleNamespace(host="192.168.0.7", name=PROD_NAMES[0]),
        wdt_database=SimpleNamespace(host="192.168.0.7", name=PROD_NAMES[1]),
        mart_database=SimpleNamespace(host="192.168.0.7", name=PROD_NAMES[2]),
    )


class StallDetectionTests(unittest.TestCase):
    """ipv6_egress_stalled：AAAA 优先 且 IPv6 不通 → True，其余全 False。"""

    def test_ipv4_first_is_not_stalled_and_never_probes(self):
        def connect(sockaddr, timeout):
            raise AssertionError("must not probe when IPv4 is preferred")

        self.assertFalse(
            ipv6_egress_stalled(getaddrinfo=_resolver([_V4, _V6]), connect=connect)
        )

    def test_aaaa_first_and_unreachable_is_stalled(self):
        def connect(sockaddr, timeout):
            raise OSError("timed out")

        self.assertTrue(
            ipv6_egress_stalled(getaddrinfo=_resolver([_V6, _V4]), connect=connect)
        )

    def test_aaaa_first_and_reachable_is_not_stalled(self):
        self.assertFalse(
            ipv6_egress_stalled(
                getaddrinfo=_resolver([_V6, _V4]), connect=lambda s, t: None
            )
        )

    def test_dns_failure_is_not_stalled(self):
        self.assertFalse(
            ipv6_egress_stalled(
                getaddrinfo=_resolver(error=socket.gaierror("no dns")),
                connect=lambda s, t: None,
            )
        )

    def test_empty_result_is_not_stalled(self):
        self.assertFalse(
            ipv6_egress_stalled(getaddrinfo=_resolver([]), connect=lambda s, t: None)
        )


class ForceIpv4Tests(unittest.TestCase):
    def test_filters_aaaa_results(self):
        fake_sock = SimpleNamespace(getaddrinfo=_resolver([_V6, _V4]))

        force_ipv4(fake_sock)

        self.assertEqual([_V4], fake_sock.getaddrinfo("example.com", 443))

    def test_idempotent_no_double_wrap(self):
        original = _resolver([_V4])
        fake_sock = SimpleNamespace(getaddrinfo=original)

        first = force_ipv4(fake_sock)
        second = force_ipv4(fake_sock)

        self.assertIs(first, second)
        self.assertIs(first._wrapped_getaddrinfo, original)

    def test_real_socket_patch_and_restore(self):
        wrapped = force_ipv4()
        try:
            self.assertTrue(getattr(socket.getaddrinfo, "_ipv4_only_baseline", False))
        finally:
            socket.getaddrinfo = wrapped._wrapped_getaddrinfo
        self.assertFalse(
            getattr(socket.getaddrinfo, "_ipv4_only_baseline", False)
        )


class AllowIpv6Tests(unittest.TestCase):
    def test_unset_empty_and_false_values(self):
        for value in (None, "", "0", "false", "no", "off"):
            with self.subTest(value=value):
                environ = {} if value is None else {ENV_ALLOW_IPV6: value}
                self.assertFalse(allow_ipv6(environ))

    def test_true_values(self):
        for value in ("1", "true", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(allow_ipv6({ENV_ALLOW_IPV6: value}))


class ApplyBaselineTests(unittest.TestCase):
    def test_default_applies_patch(self):
        fake_sock = SimpleNamespace(getaddrinfo=_resolver([_V6, _V4]))

        self.assertTrue(apply_ipv4_baseline({}, fake_sock))
        self.assertEqual([_V4], fake_sock.getaddrinfo("example.com", 443))

    def test_opt_in_skips_patch(self):
        fake_sock = SimpleNamespace(getaddrinfo=_resolver([_V6, _V4]))

        self.assertFalse(
            apply_ipv4_baseline({ENV_ALLOW_IPV6: "1"}, fake_sock)
        )
        self.assertEqual([_V6, _V4], fake_sock.getaddrinfo("example.com", 443))


class EgressGateTests(unittest.TestCase):
    """live_safety 门禁：默认不探测；显式放行 IPv6 时真探测、卡死即拒。"""

    def setUp(self):
        self.settings = _settings()
        self.environ = {"PUBLIC_DATA_TARGET": "cloud-managed"}

    def test_default_env_never_probes(self):
        def stalled():
            raise AssertionError("must not probe when IPv4 baseline is on")

        require_live_run(
            self.settings,
            live_read=True,
            confirm_local_test_write=True,
            environ=self.environ,
            stalled=stalled,
        )
        require_gateway_run(
            self.settings,
            live_send=True,
            confirm_local_test_write=True,
            environ=self.environ,
            stalled=stalled,
        )

    def test_allow_ipv6_and_stalled_rejects_live_run(self):
        environ = dict(self.environ, **{ENV_ALLOW_IPV6: "1"})
        with self.assertRaises(LiveRunRejected) as ctx:
            require_live_run(
                self.settings,
                live_read=True,
                confirm_local_test_write=True,
                environ=environ,
                stalled=lambda: True,
            )
        self.assertIn("ipv6_egress_stall", str(ctx.exception))

    def test_allow_ipv6_and_stalled_rejects_gateway_run(self):
        environ = dict(self.environ, **{ENV_ALLOW_IPV6: "1"})
        with self.assertRaises(LiveRunRejected):
            require_gateway_run(
                self.settings,
                live_send=True,
                confirm_local_test_write=True,
                environ=environ,
                stalled=lambda: True,
            )

    def test_allow_ipv6_and_healthy_egress_passes(self):
        environ = dict(self.environ, **{ENV_ALLOW_IPV6: "1"})
        require_live_run(
            self.settings,
            live_read=True,
            confirm_local_test_write=True,
            environ=environ,
            stalled=lambda: False,
        )

    def test_standalone_gate(self):
        with self.assertRaises(LiveRunRejected):
            require_ipv4_egress(
                environ={ENV_ALLOW_IPV6: "1"}, stalled=lambda: True
            )
        require_ipv4_egress(environ={}, stalled=lambda: True)  # 默认不探测


if __name__ == "__main__":
    unittest.main()
