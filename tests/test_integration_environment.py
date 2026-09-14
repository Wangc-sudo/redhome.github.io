import json
import os
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.integration.yml"
DOCKERFILE = REPOSITORY_ROOT / "docker" / "integration" / "Dockerfile"
DOCKERIGNORE_FILE = REPOSITORY_ROOT / ".dockerignore"
RUNNER_SCRIPT = REPOSITORY_ROOT / "docker" / "integration" / "run-tests.sh"


@unittest.skipIf(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Compose contracts are verified from the Docker host.",
)
class IntegrationEnvironmentContractTests(unittest.TestCase):
    def test_compose_defines_isolated_ubuntu_runner_and_persistent_mysql(self):
        completed = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        configuration = json.loads(completed.stdout)
        services = configuration["services"]
        mysql = services["mysql"]
        runner = services["test-runner"]

        # MySQL is reachable from the host (Metabase, local clients) but only
        # via loopback -- never on the LAN.
        (port_mapping,) = mysql["ports"]
        self.assertEqual("127.0.0.1", port_mapping["host_ip"])
        self.assertEqual(3306, port_mapping["target"])
        self.assertEqual("13306", port_mapping["published"])
        self.assertTrue(
            any(
                volume["type"] == "volume"
                and volume["source"] == "public-data-mysql-data"
                and volume["target"] == "/var/lib/mysql"
                for volume in mysql["volumes"]
            )
        )
        self.assertEqual("test", runner["environment"]["APP_ENV"])
        self.assertEqual("mysql", runner["environment"]["PUBLIC_DATA_RDS_HOST"])
        self.assertEqual(
            "service_healthy", runner["depends_on"]["mysql"]["condition"]
        )
        # CURDATE() drives every month window; the DB clock must share the
        # business timezone (Asia/Shanghai) that bi-web / the robots use,
        # or the Python default month and the SQL current month split at
        # each month boundary.
        self.assertEqual("Asia/Shanghai", mysql["environment"]["TZ"])
        self.assertTrue(DOCKERFILE.is_file())
        self.assertTrue(DOCKERFILE.read_text(encoding="utf-8").startswith("FROM ubuntu:24.04"))

    def test_build_context_excludes_local_sensitive_configuration(self):
        self.assertTrue(DOCKERIGNORE_FILE.is_file())
        ignored_paths = set(DOCKERIGNORE_FILE.read_text(encoding="utf-8").splitlines())

        self.assertTrue(
            {
                "config.json",
                "wdt_credentials.json",
                "config-local/",
                "common/dingtalk/test_groups.json",
                "logs/",
            }.issubset(ignored_paths)
        )

    def test_runner_executes_unittests_without_docker_access(self):
        self.assertTrue(RUNNER_SCRIPT.is_file())
        script = RUNNER_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("INTEGRATION_TEST_RUNNER=1", script)
        self.assertIn("python3 -m unittest discover -s tests -t . -v", script)
        self.assertNotIn("docker ", script)

    def test_sync_runner_is_opt_in_and_leak_free(self):
        completed = subprocess.run(
            [
                "docker", "compose",
                "-f", str(COMPOSE_FILE),
                "--profile", "live-sync",
                "config", "--format", "json",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        configuration = json.loads(completed.stdout)
        services = configuration["services"]

        # -- sync-runner contracts -------------------------------------------
        sync_runner = services["sync-runner"]

        # Must be opt-in via --profile live-sync
        self.assertEqual(sync_runner.get("profiles"), ["live-sync"])

        # Depends on healthy mysql
        self.assertEqual(
            "service_healthy",
            sync_runner["depends_on"]["mysql"]["condition"],
        )

        # Uses the same Ubuntu build image as test-runner
        self.assertEqual(
            sync_runner["build"]["dockerfile"],
            "docker/integration/Dockerfile",
        )

        # No exposed ports
        self.assertNotIn("ports", sync_runner)

        # Command includes both confirmation flags
        command = sync_runner.get("command", [])
        if isinstance(command, str):
            command_parts = command.split()
        else:
            command_parts = list(command)
        self.assertIn("--live-read", command_parts)
        self.assertIn("--confirm-local-test-write", command_parts)

        # Read-only bind mounts for manifest and credentials only
        volumes = sync_runner.get("volumes", [])
        bind_mounts = [v for v in volumes if v.get("type") == "bind"]
        self.assertEqual(len(bind_mounts), 2)
        targets = {v["target"] for v in bind_mounts}
        self.assertEqual(
            targets,
            {"/run/live-input/manifest.json", "/run/live-input/source-credentials.json"},
        )
        for mount in bind_mounts:
            self.assertTrue(mount.get("read_only", False))

        # -- test-runner is unchanged ----------------------------------------
        runner = services["test-runner"]
        self.assertFalse(runner.get("profiles"))
        runner_volumes = runner.get("volumes", [])
        runner_targets = {
            v.get("target", "") if isinstance(v, dict) else ""
            for v in runner_volumes
        }
        self.assertNotIn("/run/live-input/source-credentials.json", runner_targets)
        self.assertNotIn("/run/live-input/manifest.json", runner_targets)

    def test_extract_runner_is_opt_in_and_credential_free(self):
        completed = subprocess.run(
            [
                "docker", "compose",
                "-f", str(COMPOSE_FILE),
                "--profile", "extract",
                "config", "--format", "json",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        configuration = json.loads(completed.stdout)
        extract = configuration["services"]["extract-mart"]

        # Opt-in via --profile extract, same isolated image, no exposed ports.
        self.assertEqual(extract.get("profiles"), ["extract"])
        self.assertEqual(
            "service_healthy", extract["depends_on"]["mysql"]["condition"]
        )
        self.assertEqual(
            extract["build"]["dockerfile"], "docker/integration/Dockerfile"
        )
        self.assertNotIn("ports", extract)

        command = extract.get("command", [])
        command_parts = command.split() if isinstance(command, str) else list(command)
        self.assertIn("extract-mart", command_parts)
        self.assertIn("--confirm-local-test-write", command_parts)

        # The whole point of the extraction layer (spec section 7): it reads
        # local raw_* only, so it must never demand a live-read
        # acknowledgement nor be able to hold source credentials.
        self.assertNotIn("--live-read", command_parts)
        volumes = extract.get("volumes", [])
        targets = {
            v.get("target", "") if isinstance(v, dict) else "" for v in volumes
        }
        self.assertNotIn("/run/live-input/source-credentials.json", targets)
        self.assertNotIn("/run/live-input/manifest.json", targets)

        # The workday calendar comes from the version-controlled seed.
        environment = extract["environment"]
        self.assertEqual(
            environment.get("PUBLIC_DATA_CALENDAR_SEED"),
            "/app/docker/integration/calendar.seed.json",
        )
        self.assertEqual(environment.get("PUBLIC_DATA_SERVICE_ID"), "extract-mart")

    def _compose_config(self, *profiles):
        command = ["docker", "compose", "-f", str(COMPOSE_FILE)]
        for profile in profiles:
            command += ["--profile", profile]
        command += ["config", "--format", "json"]
        completed = subprocess.run(
            command, cwd=REPOSITORY_ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_robot_runner_is_opt_in_and_credential_free(self):
        configuration = self._compose_config("robot")
        robot = configuration["services"]["robot-hangzhou"]

        self.assertEqual(robot.get("profiles"), ["robot"])
        self.assertEqual(
            "service_healthy", robot["depends_on"]["mysql"]["condition"]
        )
        self.assertNotIn("ports", robot)

        command = robot.get("command", [])
        parts = command.split() if isinstance(command, str) else list(command)
        self.assertIn("common.daily_robot.mart_cli", parts)
        self.assertIn("once", parts)
        self.assertIn("--confirm-local-test-write", parts)

        # Business line (spec section 7): no source credentials, no external
        # calls -- no live flags, no mounts at all.
        self.assertNotIn("--live-read", parts)
        self.assertNotIn("--live-send", parts)
        self.assertFalse(robot.get("volumes"))

        environment = robot["environment"]
        self.assertEqual(environment.get("ROBOT_REGION"), "hangzhou")
        self.assertEqual(
            environment.get("PUBLIC_DATA_SERVICE_ID"), "robot-hangzhou"
        )
        self.assertEqual(
            environment.get("PUBLIC_DATA_REGION_SEED"),
            "/app/docker/integration/regions.seed.json",
        )

    def test_gateway_runner_mounts_only_dingtalk_credentials(self):
        configuration = self._compose_config("dingtalk-gateway")
        gateway = configuration["services"]["dingtalk-gateway"]

        self.assertEqual(gateway.get("profiles"), ["dingtalk-gateway"])
        self.assertNotIn("ports", gateway)

        command = gateway.get("command", [])
        parts = command.split() if isinstance(command, str) else list(command)
        self.assertIn("common.gateway.cli", parts)
        self.assertIn("run", parts)
        self.assertIn("--live-send", parts)
        self.assertIn("--confirm-local-test-write", parts)
        # One process, both gateway duties (spec section 2): outbox + stream.
        self.assertIn("--with-stream", parts)
        self.assertIn("--live-read", parts)

        # The only business-related container with DingTalk credentials:
        # the credentials file is mounted, the source manifest is not.
        targets = {
            v.get("target", "") if isinstance(v, dict) else ""
            for v in gateway.get("volumes", [])
        }
        self.assertIn("/run/live-input/source-credentials.json", targets)
        self.assertNotIn("/run/live-input/manifest.json", targets)

        environment = gateway["environment"]
        self.assertEqual(
            environment.get("PUBLIC_DATA_SERVICE_ID"), "dingtalk-gateway"
        )
        self.assertEqual(
            environment.get("PUBLIC_DATA_REGION_SEED"),
            "/app/docker/integration/regions.seed.json",
        )

    def test_pages_runner_is_opt_in_and_credential_free(self):
        configuration = self._compose_config("pages")
        pages = configuration["services"]["pages-hangzhou"]

        self.assertEqual(pages.get("profiles"), ["pages"])
        self.assertEqual(
            "service_healthy", pages["depends_on"]["mysql"]["condition"]
        )
        self.assertNotIn("ports", pages)

        command = pages.get("command", [])
        parts = command.split() if isinstance(command, str) else list(command)
        self.assertIn("common.daily_robot.mart_cli", parts)
        self.assertIn("leaderboard-html", parts)
        self.assertIn("--confirm-local-test-write", parts)
        self.assertNotIn("--live-read", parts)
        self.assertNotIn("--live-send", parts)

        # Business line (spec section 7): no credentials; the only mount is
        # the HTML output directory.
        targets = {
            v.get("target", "") if isinstance(v, dict) else ""
            for v in pages.get("volumes", [])
        }
        self.assertEqual(targets, {"/output"})

        environment = pages["environment"]
        self.assertEqual(environment.get("ROBOT_REGION"), "hangzhou")
        self.assertEqual(
            environment.get("PUBLIC_DATA_SERVICE_ID"), "pages-hangzhou"
        )

    def test_bi_web_is_opt_in_and_read_only(self):
        configuration = self._compose_config("bi-web")
        bi_web = configuration["services"]["bi-web"]

        # Opt-in via --profile bi-web: the cockpit never starts by accident.
        self.assertEqual(bi_web.get("profiles"), ["bi-web"])

        # Depends on healthy mysql.
        self.assertEqual(
            "service_healthy", bi_web["depends_on"]["mysql"]["condition"]
        )

        # 零挂载 (spec section 9): a read-only mart consumer mounts nothing --
        # no credentials, no live-input, no output directories.
        self.assertFalse(bi_web.get("volumes"))

        # The command runs the app module and never carries a --live-* flag.
        command = bi_web.get("command", [])
        parts = command.split() if isinstance(command, str) else list(command)
        self.assertEqual(["-m", "common.bi_web.app"], parts)
        self.assertFalse(any(part.startswith("--live") for part in parts))

        # Loopback-only port, the same rule as mysql 13306 -- the cockpit is
        # for the operator's browser, never for the LAN.
        (port_mapping,) = bi_web["ports"]
        self.assertEqual("127.0.0.1", port_mapping["host_ip"])
        self.assertEqual(8080, port_mapping["target"])
        self.assertEqual("18080", port_mapping["published"])

        environment = bi_web["environment"]
        self.assertEqual(
            environment.get("PUBLIC_DATA_CONFIG"),
            "/app/docker/integration/public-data-test-config.json",
        )
        # bi-web writes nowhere at all, so it never needs the runner's
        # write-gate acknowledgement.
        self.assertNotIn("INTEGRATION_TEST_RUNNER", environment)

    def test_bi_web_redis_cache_is_opt_in_and_fail_open(self):
        # 阶段 2 (2026-09-14 cache spec): Redis 是可选增强，不是硬依赖。
        # 默认编排里没有它 —— 不配 profile 时 bi-web 用进程内缓存兜底。
        default_services = self._compose_config()["services"]
        self.assertNotIn("redis", default_services)

        configuration = self._compose_config("bi-web", "bi-web-redis")
        redis = configuration["services"]["redis"]
        bi_web = configuration["services"]["bi-web"]

        # Opt-in via --profile bi-web-redis, pinned image.
        self.assertEqual(["bi-web-redis"], redis.get("profiles"))
        self.assertEqual("redis:7-alpine", redis["image"])

        # Loopback-only port, the same rule as mysql 13306 / bi-web 18080.
        (port_mapping,) = redis["ports"]
        self.assertEqual("127.0.0.1", port_mapping["host_ip"])
        self.assertEqual(6379, port_mapping["target"])
        self.assertEqual("16379", port_mapping["published"])

        # 缓存挂 != 看板挂：bi-web 绝不 depends_on redis（fail-open，
        # miss 即 mart 直查）；URL 旋钮已注入，空串缺省 = 进程内后端。
        self.assertNotIn("redis", bi_web.get("depends_on", {}))
        self.assertIn("PUBLIC_DATA_REDIS_URL", bi_web["environment"])
        # Deliberate deviation from spec section 9 (recorded in the plan):
        # the spec also asks for "no raw-database environment variable
        # references" here, but Settings.from_environment requires all nine
        # variables (including the raw dingtalk/wdt database names), so a
        # compose service without them could never boot.  The zero-raw rule
        # is enforced at the code level instead: the default db_connector
        # only ever passes settings.mart_database to connect().


if __name__ == "__main__":
    unittest.main()
