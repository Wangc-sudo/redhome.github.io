import fnmatch
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "ops" / "channel-release.json"


class ChannelReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_references_existing_files_and_unique_services(self):
        services = self.manifest["scheduler_services"]
        self.assertEqual(len(services), len(set(services)))
        self.assertIn("sync-channel-sales", services)
        self.assertIn("extract-channel", services)
        self.assertIn("channel-order-daily", services)
        self.assertIn("channel-missing-check", services)
        for relative in [*self.manifest["ci_paths"], *self.manifest["tests"]]:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertEqual(sorted(self.manifest["tests"]), sorted(set(self.manifest["tests"])))

    def test_every_manifest_service_is_registered_and_has_a_command(self):
        seed = (ROOT / "docker/integration/pipelines.seed.yaml").read_text(encoding="utf-8")
        seed_services = set(re.findall(r"^([a-z][a-z0-9-]*):$", seed, re.MULTILINE))
        scheduler = (ROOT / "common/public_data/scheduler.py").read_text(encoding="utf-8")
        for service_id in self.manifest["scheduler_services"]:
            with self.subTest(service=service_id):
                self.assertIn(service_id, seed_services)
        for service_id in (
            "sync-channel-sales", "extract-channel", "dq-check-channel",
            "channel-order-daily", "channel-missing-check", "freshness-check",
        ):
            self.assertIn(f'"{service_id}"', scheduler)
        for family in ("_ROBOT_REGIONS", "_PAGES_REGIONS", "_LEADERBOARD_REGIONS"):
            self.assertIn(family, scheduler)

    def test_channel_workflow_covers_manifest_paths_and_always_reports_gate(self):
        workflow = (ROOT / ".github/workflows/channel-ci.yml").read_text(encoding="utf-8")
        patterns = re.findall(r"^\s+- '([^']+)'\s*$", workflow, re.MULTILINE)
        for relative in self.manifest["ci_paths"]:
            with self.subTest(path=relative):
                self.assertTrue(
                    any(fnmatch.fnmatch(relative, pattern) for pattern in patterns),
                    f"channel-ci.yml 未监听 {relative}",
                )
        self.assertIn("if: always()", workflow)
        self.assertIn("channel-gate:", workflow)
        self.assertIn("success(no-op)", workflow)
        self.assertNotIn("needs.channel-tests.result }}\" == \"skipped\" ||", workflow)

    def test_scheduler_ownership_is_split_between_compose_projects(self):
        main_compose = (ROOT / "docker/cloud/docker-compose.dops.yml").read_text(encoding="utf-8")
        channel_compose = (ROOT / "docker/cloud/docker-compose.channel.yml").read_text(encoding="utf-8")
        self.assertIn("SCHEDULER_SERVICE_EXCLUDE_FILE: /opt/dops/repo/ops/channel-release.json", main_compose)
        self.assertIn("SCHEDULER_SERVICE_INCLUDE_FILE: /opt/dops/channel-repo/ops/channel-release.json", channel_compose)
        self.assertIn("image: dops-channel-app:latest", channel_compose)
        self.assertIn("/opt/dops/channel-repo:/opt/dops/channel-repo:ro", channel_compose)
        self.assertIn("dops-channel-scheduler", channel_compose)
        for seed in ("CALENDAR", "ORG", "REGION", "BI"):
            self.assertIn(
                f"PUBLIC_DATA_{seed}_SEED: /opt/dops/channel-repo/",
                channel_compose,
            )
        self.assertNotIn("with-stream", channel_compose)
        self.assertNotIn("kb-gateway:", channel_compose)

    def test_channel_cd_has_independent_state_health_and_real_rollback(self):
        script = (ROOT / "ops/deploy-channel.sh").read_text(encoding="utf-8")
        self.assertIn("/opt/dops/channel-repo", script)
        self.assertIn("/var/run/dops-channel-deploy.lock", script)
        self.assertIn("/opt/dops/channel-deploy-status.json", script)
        self.assertIn('RELEASE_ID="channel-$(date +%Y%m%d%H%M%S)"', script)
        self.assertIn("python3 ops/channel_health.py", script)
        self.assertIn('apply "$LOCAL_SHA"', script)
        self.assertIn("rebuild dops-channel-app:latest", script)
        self.assertIn("docker image inspect dops-channel-app:latest", script)
        self.assertIn("Settings.from_environment()", script)
        self.assertIn("status=failed", script)
        self.assertEqual(1, script.count("DEPS_CHANGED=0"))
        self.assertNotIn("publish-pipelines --", script)
        for unit in ("dops-channel-deploy.service", "dops-channel-deploy.timer"):
            self.assertTrue((ROOT / "ops" / unit).is_file())

    def test_channel_health_is_read_only_and_checks_all_owned_services(self):
        health = (ROOT / "ops/channel_health.py").read_text(encoding="utf-8")
        self.assertIn("set(actual) != set(expected)", health)
        self.assertIn("command_spec", health)
        self.assertIn("SCHEDULER_HEARTBEAT_PATH", health)
        self.assertIn("Settings.from_environment()", health)
        self.assertNotIn("confirm-local-test-write", health)
        self.assertNotRegex(health, r"(?i)\bINSERT\s+")
        self.assertNotRegex(health, r"(?i)\bUPDATE\s+")


if __name__ == "__main__":
    unittest.main()
