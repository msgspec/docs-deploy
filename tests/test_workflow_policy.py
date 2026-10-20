import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        self.jobs = dict(
            re.findall(
                r"(?ms)^  (metadata|preview|production):\n(.*?)(?=^  [a-z]+:|\Z)",
                self.workflow,
            )
        )

    def test_dispatch_has_only_run_identity_inputs(self):
        inputs = self.workflow.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[
            0
        ]
        self.assertEqual(
            re.findall(r"^      ([a-z-]+):$", inputs, re.MULTILINE),
            ["source-run-id", "source-run-attempt"],
        )
        self.assertEqual(inputs.count("required: true"), 2)
        self.assertEqual(inputs.count("type: string"), 2)
        self.assertIn('test "$GITHUB_REF" = refs/heads/main', self.workflow)

    def test_all_external_actions_are_commit_pinned(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for action in re.findall(r"uses: (\S+)", path.read_text()):
                with self.subTest(path=path.name, action=action):
                    self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_only_production_can_write_and_lock_covers_snapshot(self):
        self.assertIn("permissions: {}", self.workflow)
        self.assertNotIn("contents: write", self.jobs["metadata"])
        self.assertNotIn("contents: write", self.jobs["preview"])
        self.assertIn("contents: write", self.jobs["production"])
        self.assertIn(
            "group: docs-production\n      cancel-in-progress: false",
            self.jobs["production"],
        )
        self.assertIn(
            "run: python scripts/publish.py snapshot", self.jobs["production"]
        )
        self.assertIn(
            "group: docs-preview-${{ needs.metadata.outputs.branch }}\n      cancel-in-progress: true",
            self.jobs["preview"],
        )

    def test_source_tokens_are_explicitly_read_only_and_source_scoped(self):
        for job in self.jobs.values():
            self.assertIn("repositories: msgspec\n", job)
            for permission in ("actions", "contents", "pull-requests"):
                self.assertIn(f"permission-{permission}: read", job)
            self.assertNotRegex(job, r"permission-[a-z-]+: write")

    def test_no_source_checkout_or_artifact_execution(self):
        self.assertEqual(self.workflow.count("repository: msgspec/docs-deploy"), 4)
        self.assertEqual(self.workflow.count("persist-credentials: false"), 4)
        self.assertNotIn("repository: msgspec/msgspec", self.workflow)
        for job in (self.jobs["preview"], self.jobs["production"]):
            self.assertIn("pages deploy ${{ runner.temp }}/docs-data/site", job)
            self.assertIn("--project-name=msgspec", job)
            self.assertIn("wranglerVersion: 4.98.0", job)
            self.assertIn("python scripts/publish.py revalidate", job)
            self.assertIn("name: docs-deployment", job)
            self.assertIn("retention-days: 7", job)
            self.assertNotIn("workingDirectory:", job)
            self.assertNotIn("preCommands:", job)
            self.assertNotIn("postCommands:", job)


if __name__ == "__main__":
    unittest.main()
