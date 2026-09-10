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

        self.assertNotIn("ports", mysql)
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


if __name__ == "__main__":
    unittest.main()
