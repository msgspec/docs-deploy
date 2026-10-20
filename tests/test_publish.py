import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import publish

PREFIX = "/repos/msgspec/msgspec"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def fixtures():
    run = {
        "id": 500,
        "run_attempt": 2,
        "workflow_id": 123,
        "path": publish.WORKFLOW,
        "repository": {"id": 10},
        "status": "completed",
        "conclusion": "success",
        "event": "pull_request",
        "head_sha": SHA,
        "head_branch": "docs-fix",
        "head_repository": {"id": 20, "owner": {"login": "contributor"}},
        "pull_requests": [{"number": 42}],
        "run_started_at": "2026-10-20T10:00:00Z",
        "created_at": "2026-10-20T09:00:00Z",
    }
    pr = {
        "number": 42,
        "state": "open",
        "base": {"ref": "main", "repo": {"id": 10}},
        "head": {"ref": "docs-fix", "sha": SHA, "repo": {"id": 20}},
    }
    artifact = {
        "id": 700,
        "name": "docs-site",
        "expired": False,
        "digest": "sha256:" + "1" * 64,
        "created_at": "2026-10-20T10:01:00Z",
        "workflow_run": {
            "id": 500,
            "repository_id": 10,
            "head_sha": SHA,
            "head_repository_id": 20,
            "head_branch": "docs-fix",
        },
    }
    return run, pr, artifact


class FakeAPI:
    def __init__(self):
        run, pr, artifact = fixtures()
        self.responses = {
            PREFIX: {"id": 10, "full_name": "msgspec/msgspec"},
            PREFIX + "/actions/workflows/docs-pr.yml": {
                "id": 123,
                "path": publish.WORKFLOW,
            },
            PREFIX + "/actions/runs/500": copy.deepcopy(run),
            PREFIX + "/actions/runs/500/attempts/2": run,
            PREFIX + "/pulls/42": pr,
            PREFIX + "/git/ref/heads/main": {"object": {"sha": SHA}},
        }
        self.lists = {
            PREFIX + "/actions/runs/500/artifacts": [artifact],
            PREFIX + f"/commits/{SHA}/pulls": [pr],
            PREFIX + "/pulls": [pr],
        }
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path))
        return copy.deepcopy(self.responses[path])

    def pages(self, path, key=None, params=None):
        self.calls.append(("pages", path, key, params))
        return iter(copy.deepcopy(self.lists[path]))

    def change_run(self, **updates):
        self.responses[PREFIX + "/actions/runs/500"].update(copy.deepcopy(updates))
        self.responses[PREFIX + "/actions/runs/500/attempts/2"].update(
            copy.deepcopy(updates)
        )


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def resolve(self):
        return publish.resolve(self.api, "500", "2")

    def test_fork_preview_exact_identity(self):
        plan = self.resolve()
        self.assertEqual(plan["target"], "preview")
        self.assertEqual(plan["branch"], "pr-42")
        self.assertEqual(plan["artifact_id"], 700)
        self.assertEqual(plan["source_sha"], SHA)

    def test_empty_fork_associations_use_commit_pulls(self):
        self.api.change_run(pull_requests=[])
        self.assertEqual(self.resolve()["branch"], "pr-42")
        self.assertIn(
            ("pages", PREFIX + f"/commits/{SHA}/pulls", None, None), self.api.calls
        )

    def test_empty_commit_associations_use_head_owner_and_branch(self):
        self.api.change_run(pull_requests=[])
        self.api.lists[PREFIX + f"/commits/{SHA}/pulls"] = []
        self.assertEqual(self.resolve()["branch"], "pr-42")
        self.assertIn(
            (
                "pages",
                PREFIX + "/pulls",
                None,
                {"state": "open", "base": "main", "head": "contributor:docs-fix"},
            ),
            self.api.calls,
        )

    def test_ambiguous_fork_pull_requests_fail(self):
        self.api.change_run(pull_requests=[])
        second = copy.deepcopy(self.api.responses[PREFIX + "/pulls/42"])
        second["number"] = 43
        self.api.responses[PREFIX + "/pulls/43"] = second
        self.api.lists[PREFIX + f"/commits/{SHA}/pulls"].append(second)
        with self.assertRaisesRegex(publish.Invalid, "exactly one"):
            self.resolve()

    def test_missing_fork_pull_requests_fail(self):
        self.api.change_run(pull_requests=[])
        self.api.lists[PREFIX + f"/commits/{SHA}/pulls"] = []
        self.api.lists[PREFIX + "/pulls"] = []
        with self.assertRaisesRegex(publish.Invalid, "exactly one"):
            self.resolve()

    def test_closed_stale_or_mismatched_pull_request_fails(self):
        for field, value in (
            ("state", "closed"),
            ("head.sha", OTHER_SHA),
            ("head.ref", "other"),
            ("head.repo.id", 99),
            ("base.ref", "release"),
            ("base.repo.id", 99),
        ):
            with self.subTest(field=field):
                api = FakeAPI()
                target = api.responses[PREFIX + "/pulls/42"]
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                with self.assertRaisesRegex(publish.Invalid, "exactly one"):
                    publish.resolve(api, 500, 2)

    def test_main_push_routes_to_fixed_production_branch(self):
        self.api.change_run(
            event="push", head_branch="main", head_repository={"id": 10}
        )
        linked = self.api.lists[PREFIX + "/actions/runs/500/artifacts"][0][
            "workflow_run"
        ]
        linked.update(head_branch="main", head_repository_id=10)
        plan = self.resolve()
        self.assertEqual(
            (plan["target"], plan["branch"], plan["pr"]),
            ("production", "cf-pages", None),
        )

    def test_wrong_push_branch_or_repository_fails(self):
        for updates in ({"event": "push"}, {"event": "push", "head_branch": "main"}):
            with self.subTest(updates=updates):
                self.api = FakeAPI()
                self.api.change_run(**updates)
                with self.assertRaisesRegex(publish.Invalid, "Production requires"):
                    self.resolve()

    def test_unapproved_events_fail(self):
        for event in (
            "workflow_dispatch",
            "pull_request_target",
            "workflow_run",
            "schedule",
        ):
            with self.subTest(event=event):
                self.api.change_run(event=event)
                with self.assertRaisesRegex(
                    publish.Invalid, "Only pull_request and push"
                ):
                    self.resolve()

    def test_stale_attempt_and_unsuccessful_run_fail(self):
        for updates in (
            {"run_attempt": 3},
            {"conclusion": "failure"},
            {"status": "in_progress"},
        ):
            with self.subTest(updates=updates):
                self.api = FakeAPI()
                self.api.responses[PREFIX + "/actions/runs/500"].update(updates)
                with self.assertRaises(publish.Invalid):
                    self.resolve()

    def test_wrong_workflow_or_source_repository_fails(self):
        for updates in (
            {"workflow_id": 999},
            {"path": ".github/workflows/other.yml"},
            {"repository": {"id": 99}},
            {"head_sha": "HEAD"},
        ):
            with self.subTest(updates=updates):
                self.api = FakeAPI()
                self.api.change_run(**updates)
                with self.assertRaises(publish.Invalid):
                    self.resolve()

    def test_attempt_and_latest_metadata_must_match(self):
        self.api.responses[PREFIX + "/actions/runs/500/attempts/2"]["head_sha"] = (
            OTHER_SHA
        )
        with self.assertRaisesRegex(publish.Invalid, "metadata changed"):
            self.resolve()

    def test_invalid_ids_fail_before_api_access(self):
        for value in ("0", "-1", "1.0", " 500", "500\n", "500; echo unsafe"):
            with self.subTest(value=value), self.assertRaises(publish.Invalid):
                publish.resolve(self.api, value, 2)
        self.assertEqual(self.api.calls, [])

    def test_missing_or_duplicate_artifacts_fail(self):
        key = PREFIX + "/actions/runs/500/artifacts"
        for artifacts in ([], self.api.lists[key] * 2):
            with self.subTest(count=len(artifacts)):
                self.api.lists[key] = artifacts
                with self.assertRaisesRegex(publish.Invalid, "Exactly one docs-site"):
                    self.resolve()

    def test_link_only_second_attempt_reuses_same_run_artifact(self):
        artifact = self.api.lists[PREFIX + "/actions/runs/500/artifacts"][0]
        artifact["created_at"] = "2026-10-20T09:01:00Z"
        plan = self.resolve()
        self.assertEqual(plan["source_run_attempt"], 2)
        self.assertEqual(plan["artifact_id"], 700)
        self.assertEqual(plan["artifact_digest"], artifact["digest"])
        self.assertEqual(plan["source_sha"], SHA)

    def test_artifact_before_original_run_is_rejected(self):
        artifact = self.api.lists[PREFIX + "/actions/runs/500/artifacts"][0]
        artifact["created_at"] = "2026-10-20T08:59:59Z"
        with self.assertRaisesRegex(publish.Invalid, "predates the source run"):
            self.resolve()

    def test_artifact_binding_expiration_and_digest_are_required(self):
        for field, value in (
            ("expired", True),
            ("digest", None),
            ("digest", "sha256:bad"),
            ("workflow_run.id", 999),
            ("workflow_run.repository_id", 999),
            ("workflow_run.head_sha", OTHER_SHA),
            ("workflow_run.head_repository_id", 999),
            ("workflow_run.head_branch", "other"),
        ):
            with self.subTest(field=field, value=value):
                api = FakeAPI()
                target = api.lists[PREFIX + "/actions/runs/500/artifacts"][0]
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                with self.assertRaises(publish.Invalid):
                    publish.resolve(api, 500, 2)


class APITests(unittest.TestCase):
    def test_signed_archive_request_does_not_receive_api_credentials(self):
        api = publish.GitHub("test-token")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "artifact.zip"
            redirect = HTTPError(
                "https://api.github.com",
                302,
                "Found",
                {"Location": "https://artifact.example.test/signed?token=opaque"},
                None,
            )
            with (
                patch.object(publish, "build_opener") as opener,
                patch.object(
                    publish, "urlopen", return_value=io.BytesIO(b"zip")
                ) as download,
            ):
                opener.return_value.open.side_effect = redirect
                api.download(700, destination)
            self.assertEqual(destination.read_bytes(), b"zip")
            self.assertEqual(
                opener.return_value.open.call_args.args[0].get_header("Authorization"),
                "Bearer test-token",
            )
            self.assertIsNone(download.call_args.args[0].get_header("Authorization"))

    def test_pagination_does_not_drop_later_results(self):
        api = publish.GitHub("test-token")
        with patch.object(api, "get", side_effect=[list(range(100)), [100]]) as get:
            self.assertEqual(
                list(api.pages(PREFIX + "/pulls", params={"head": "fork:a/b"})),
                list(range(101)),
            )
        self.assertIn("page=2", get.call_args.args[0])
        self.assertIn("head=fork%3Aa%2Fb", get.call_args.args[0])

    def test_api_is_fixed_to_source_repository(self):
        api = publish.GitHub("test-token")
        for path in ("https://evil.test", "/repos/other/repo", PREFIX + "-other"):
            with self.subTest(path=path), self.assertRaises(publish.Invalid):
                api.request(path)
        request = api.request(PREFIX)
        self.assertEqual(request.full_url, "https://api.github.com" + PREFIX)


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries):
        path = self.root / "site.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, data in entries:
                if isinstance(name, str):
                    entry = zipfile.ZipInfo(name)
                    # Preserve raw hostile backslashes when tests run on Windows.
                    entry.filename = name
                else:
                    entry = name
                bundle.writestr(entry, data)
        return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def validate(self, entries):
        archive, digest = self.archive(entries)
        publish.validate_zip(archive, digest, self.root / "site")

    def test_static_site_extracts_away_from_workflow(self):
        self.validate(
            [
                ("index.html", "site"),
                ("assets/a.js", "static bytes"),
                ("wrangler.toml", "malicious config is data"),
                ("_headers", "/*\n X-Test: yes"),
            ]
        )
        self.assertEqual((self.root / "site/assets/a.js").read_text(), "static bytes")

    def test_digest_mismatch_fails_before_extraction(self):
        archive, _ = self.archive([("index.html", "site")])
        with self.assertRaisesRegex(publish.Invalid, "digest mismatch"):
            publish.validate_zip(archive, "sha256:" + "0" * 64, self.root / "site")
        self.assertFalse((self.root / "site").exists())

    def test_unsafe_paths_fail_before_extraction(self):
        for name in (
            "../escape",
            "/absolute",
            "C:/drive",
            "C:drive",
            "\\server\\share",
            "assets/../escape",
            "assets\\escape",
            "assets//escape",
            "./index.html",
            ".git/config",
            "a/.GIT/config",
            ".github/workflows/publish.yml",
            "a:stream",
        ):
            with self.subTest(name=name), self.assertRaises(publish.Invalid):
                self.validate([("index.html", "site"), (name, "bad")])
            self.assertFalse((self.root / "site").exists())

    def test_worker_files_worker_directories_and_functions_fail(self):
        for name in (
            "_worker.js",
            "_worker.js/",
            "_worker.js/index.js",
            "functions/hello.js",
            "functions",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(publish.Invalid, "Workers or Functions"),
            ):
                self.validate([("index.html", "site"), (name, "bad")])

    def test_symlinks_and_special_files_fail(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK):
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = (mode | 0o777) << 16
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(publish.Invalid, "symbolic links"),
            ):
                self.validate([("index.html", "site"), (entry, "../outside")])

    def test_root_index_is_required(self):
        with self.assertRaisesRegex(publish.Invalid, "root index.html"):
            self.validate([("nested/index.html", "site")])

    def test_duplicate_entries_and_conflicting_paths_fail(self):
        for entries in (
            [("index.html", "one"), ("index.html", "two")],
            [("index.html", "site"), ("asset", "file"), ("asset/child", "bad")],
        ):
            with (
                self.subTest(entries=entries),
                self.assertRaises(publish.Invalid),
                patch("warnings.warn"),
            ):
                self.validate(entries)

    def test_existing_extraction_directory_fails(self):
        destination = self.root / "site"
        destination.mkdir()
        (destination / "keep").write_text("untouched")
        with self.assertRaisesRegex(publish.Invalid, "fresh data directory"):
            self.validate([("index.html", "site")])
        self.assertEqual((destination / "keep").read_text(), "untouched")

    def test_archive_resource_limits_fail_before_extraction(self):
        with (
            patch.object(publish, "MAX_EXTRACTED_BYTES", 3),
            self.assertRaisesRegex(publish.Invalid, "1 GiB"),
        ):
            self.validate([("index.html", "four")])
        with (
            patch.object(publish, "MAX_FILES", 1),
            self.assertRaisesRegex(publish.Invalid, "entry count"),
        ):
            self.validate([("index.html", "one"), ("a", "two")])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "trusted"
        self.workspace.mkdir()
        self.data = self.root / "docs-data"
        self.data.mkdir()
        self.plan = {
            "source_run_id": 500,
            "source_run_attempt": 2,
            "source_sha": SHA,
            "target": "production",
            "branch": "cf-pages",
            "pr": None,
            "artifact_id": 700,
            "artifact_digest": "sha256:" + "1" * 64,
        }
        self.plan_path = self.data / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan))
        self.env = {
            "GITHUB_REPOSITORY": publish.PUBLISHER,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_RUN_ID": "900",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_OUTPUT": str(self.root / "output"),
            "SOURCE_TOKEN": "test-token",
            "SOURCE_RUN_ID": "500",
            "SOURCE_RUN_ATTEMPT": "2",
        }

    def command(self, command, **env):
        with (
            patch.dict(os.environ, dict(self.env, **env)),
            patch.object(sys, "argv", ["publish.py", command]),
            redirect_stdout(io.StringIO()),
        ):
            publish.main()

    def test_non_main_or_other_repository_is_rejected_before_access(self):
        for env in (
            {"GITHUB_REF": "refs/heads/untrusted"},
            {"GITHUB_REPOSITORY": "fork/docs-deploy"},
        ):
            with (
                self.subTest(env=env),
                self.assertRaisesRegex(publish.Invalid, "only in"),
            ):
                self.command("metadata", **env)

    def test_changed_plan_fails_immediate_revalidation(self):
        with (
            patch.object(
                publish, "resolve", return_value=dict(self.plan, artifact_id=701)
            ),
            self.assertRaisesRegex(publish.Invalid, "changed after download"),
        ):
            self.command("revalidate")

    def test_outdated_main_skips_production(self):
        api = FakeAPI()
        api.responses[PREFIX + "/git/ref/heads/main"]["object"]["sha"] = OTHER_SHA
        with (
            patch.object(publish, "GitHub", return_value=api),
            patch.object(publish, "resolve", return_value=self.plan),
        ):
            self.command("revalidate")
        self.assertEqual((self.root / "output").read_text(), "publish=false\n")

    def test_identical_snapshot_skips_and_changes_publish(self):
        for name in (self.data / "site", self.workspace / "snapshot"):
            name.mkdir()
            (name / "index.html").write_text("same")
        with (
            patch.object(publish, "GitHub", return_value=FakeAPI()),
            patch.object(publish, "resolve", return_value=self.plan),
        ):
            self.command("revalidate")
            (self.data / "site/index.html").write_text("changed")
            self.command("revalidate")
        self.assertEqual(
            (self.root / "output").read_text(), "publish=false\npublish=true\n"
        )

    def test_production_result_schema_and_skip(self):
        self.command("result", PUBLISHED="false")
        self.assertEqual(
            json.loads((self.data / "result.json").read_text()),
            {
                "source_run_id": 500,
                "source_run_attempt": 2,
                "source_sha": SHA,
                "target": "production",
                "publisher_run_id": 900,
                "url": "https://msgspec.dev",
                "published": False,
            },
        )

    def test_preview_result_requires_its_exact_alias(self):
        self.plan.update(target="preview", branch="pr-42", pr=42)
        self.plan_path.write_text(json.dumps(self.plan))
        for url in (
            "https://evil.test",
            "https://pr-43.msgspec.pages.dev",
            "http://pr-42.msgspec.pages.dev",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(publish.Invalid, "expected msgspec"),
            ):
                self.command("result", PUBLISHED="true", DEPLOYMENT_URL=url)
        self.command(
            "result",
            PUBLISHED="true",
            DEPLOYMENT_URL="https://pr-42.msgspec.pages.dev/",
        )
        self.assertEqual(
            json.loads((self.data / "result.json").read_text())["url"],
            "https://pr-42.msgspec.pages.dev",
        )


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_exact_data_without_filters_or_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            site = root / "site"
            snapshot.mkdir()
            site.mkdir()
            git = ["git", "-C", str(snapshot)]
            subprocess.run(
                git + ["init", "--initial-branch=cf-pages", "--quiet"], check=True
            )
            subprocess.run(
                git
                + [
                    "-c",
                    "user.name=seed",
                    "-c",
                    "user.email=seed@example.test",
                    "commit",
                    "--allow-empty",
                    "--quiet",
                    "-m",
                    "seed",
                ],
                check=True,
            )
            subprocess.run(
                git
                + ["config", "filter.forbidden.clean", "this-command-must-never-exist"],
                check=True,
            )
            subprocess.run(
                git + ["config", "filter.forbidden.required", "true"], check=True
            )
            hook = snapshot / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 97\n")
            hook.chmod(0o755)
            (site / ".gitattributes").write_text("* filter=forbidden\n")
            (site / ".gitignore").write_text("ignored.html\n")
            (site / "ignored.html").write_text("still included\n")
            (site / "index.html").write_bytes(b"exact\r\nbytes\n")
            commit = publish.snapshot_commit(site, snapshot, SHA)
            files = subprocess.check_output(
                git + ["ls-tree", "--name-only", commit], text=True
            ).splitlines()
            self.assertEqual(
                files, [".gitattributes", ".gitignore", "ignored.html", "index.html"]
            )
            self.assertEqual(
                subprocess.check_output(git + ["show", commit + ":index.html"]),
                b"exact\r\nbytes\n",
            )
            author = subprocess.check_output(
                git + ["show", "-s", "--format=%an <%ae>", commit], text=True
            ).strip()
            self.assertEqual(
                author,
                "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
            )


if __name__ == "__main__":
    unittest.main()
