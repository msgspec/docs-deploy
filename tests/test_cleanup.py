import copy
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import cleanup

PROJECT_ID = "project-id"
ACCOUNT_ID = "a" * 32


def deployment(name="old", branch="cf-pages", environment="production", aliases=None):
    return {
        "id": name,
        "project_id": PROJECT_ID,
        "project_name": "msgspec",
        "environment": environment,
        "deployment_trigger": {"metadata": {"branch": branch}},
        "aliases": aliases,
        "latest_stage": {"name": "deploy", "status": "success"},
    }


def preview(name="preview", branch="pr-42", aliased=True):
    aliases = [f"https://{branch}.msgspec.pages.dev"] if aliased else []
    return deployment(name, branch, "preview", aliases)


def pull_request(number=42):
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "base": {"ref": "main", "repo": {"id": 10, "full_name": cleanup.SOURCE}},
    }


def page(items, number=1, total=None):
    total = len(items) if total is None else total
    return {
        "success": True,
        "errors": [],
        "result": items,
        "result_info": {
            "page": number,
            "per_page": 25,
            "count": len(items),
            "total_count": total,
            "total_pages": (total + 24) // 25,
        },
    }


class FakeSource:
    def __init__(self, *states):
        self.states = list(states or [set()])
        self.calls = 0

    def open_prs(self):
        self.calls += 1
        value = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(value, Exception):
            raise value
        return value


class FakeCloudflare:
    def __init__(self, *items):
        self.snapshot = {"live": deployment("live")}
        self.snapshot.update((item["id"], item) for item in items)
        self.current = copy.deepcopy(self.snapshot)
        self.canonicals = ["live"]
        self.calls = []
        self.fail_delete = False

    def project(self):
        value = (
            self.canonicals.pop(0) if len(self.canonicals) > 1 else self.canonicals[0]
        )
        self.calls.append(("project", value))
        return PROJECT_ID, value

    def deployments(self, project_id):
        self.calls.append(("snapshot", project_id))
        return copy.deepcopy(self.snapshot)

    def get(self, deployment_id, project_id):
        self.calls.append(("get", deployment_id))
        return copy.deepcopy(self.current[deployment_id])

    def delete(self, deployment_id, force):
        self.calls.append(("delete", deployment_id, force))
        if self.fail_delete:
            raise cleanup.Invalid("Deletion failed.")


class RetentionTests(unittest.TestCase):
    def sweep(self, cloudflare, source=None):
        with redirect_stdout(io.StringIO()):
            return cleanup.sweep(cloudflare, source or FakeSource({42}))

    def deleted(self, cloudflare):
        return [call[1:] for call in cloudflare.calls if call[0] == "delete"]

    def test_only_current_production_and_serving_open_pr_previews_remain(self):
        cloudflare = FakeCloudflare(
            deployment(),
            preview("open"),
            preview("older", aliased=False),
            preview("closed", "pr-43"),
            preview("merged", "pr-44"),
            preview("nonexistent", "pr-99999"),
            preview("non-main", "pr-45"),
            preview("legacy", "old-branch"),
        )
        result = self.sweep(cloudflare)
        self.assertEqual(result, dict(listed=9, deleted=7, retained=2))
        self.assertEqual(
            self.deleted(cloudflare),
            [
                ("old", False),
                ("older", False),
                ("closed", True),
                ("merged", True),
                ("nonexistent", True),
                ("non-main", True),
                ("legacy", True),
            ],
        )

    def test_an_open_pr_does_not_keep_unaliased_history(self):
        cloudflare = FakeCloudflare(preview(aliased=False))
        self.assertEqual(self.sweep(cloudflare)["deleted"], 1)

    def test_all_in_progress_stages_are_retained(self):
        for stage in ("queued", "initialize", "clone_repo", "build", "deploy"):
            for status in ("idle", "active", "success"):
                if stage == "deploy" and status == "success":
                    continue
                with self.subTest(stage=stage, status=status):
                    item = preview()
                    item["latest_stage"].update(name=stage, status=status)
                    cloudflare = FakeCloudflare(item)
                    self.assertEqual(self.sweep(cloudflare, FakeSource())["deleted"], 0)

    def test_failed_canceled_and_skipped_deployments_are_removed(self):
        for status in ("failure", "canceled", "skipped"):
            with self.subTest(status=status):
                item = preview(aliased=False)
                item["latest_stage"].update(name="build", status=status)
                cloudflare = FakeCloudflare(item)
                self.assertEqual(self.sweep(cloudflare)["deleted"], 1)

    def test_production_rollback_between_snapshot_and_delete_is_protected(self):
        cloudflare = FakeCloudflare(deployment())
        cloudflare.canonicals = ["live", "old"]
        self.assertEqual(self.sweep(cloudflare)["deleted"], 0)
        self.assertEqual(cloudflare.calls[-1], ("project", "old"))

    def test_current_project_is_rechecked_immediately_before_each_delete(self):
        cloudflare = FakeCloudflare(deployment(), preview("closed", "pr-43"))
        self.sweep(cloudflare)
        for index, call in enumerate(cloudflare.calls):
            if call[0] == "delete":
                self.assertEqual(cloudflare.calls[index - 1], ("project", "live"))

    def test_reopened_pr_is_protected_by_fresh_authoritative_source_state(self):
        cloudflare = FakeCloudflare(preview())
        source = FakeSource(set(), {42})
        self.assertEqual(self.sweep(cloudflare, source)["deleted"], 0)
        self.assertEqual(source.calls, 2)

    def test_new_alias_assigned_after_snapshot_is_protected(self):
        cloudflare = FakeCloudflare(preview(aliased=False))
        cloudflare.current["preview"] = preview()
        source = FakeSource(set(), {42})
        self.assertEqual(self.sweep(cloudflare, source)["deleted"], 0)

    def test_retried_in_progress_deployment_is_protected(self):
        cloudflare = FakeCloudflare(deployment())
        cloudflare.current["old"]["latest_stage"]["status"] = "active"
        self.assertEqual(self.sweep(cloudflare)["deleted"], 0)

    def test_initial_and_recheck_source_errors_fail_closed(self):
        for states in (
            (cleanup.Invalid("Authentication failed."),),
            (set(), cleanup.Invalid("Authentication failed.")),
        ):
            with self.subTest(states=states):
                cloudflare = FakeCloudflare(preview())
                with self.assertRaisesRegex(cleanup.Invalid, "Authentication"):
                    self.sweep(cloudflare, FakeSource(*states))
                self.assertEqual(self.deleted(cloudflare), [])

    def test_ambiguous_aliases_fail_before_any_deletion(self):
        cloudflare = FakeCloudflare(deployment(), preview("one"), preview("two"))
        with self.assertRaisesRegex(cleanup.Invalid, "same alias"):
            self.sweep(cloudflare)
        self.assertEqual(self.deleted(cloudflare), [])

    def test_missing_canonical_in_snapshot_fails_closed(self):
        cloudflare = FakeCloudflare(deployment())
        del cloudflare.snapshot["live"]
        with self.assertRaisesRegex(cleanup.Invalid, "omitted canonical"):
            self.sweep(cloudflare)
        self.assertEqual(self.deleted(cloudflare), [])

    def test_mutated_identity_fails_closed(self):
        cloudflare = FakeCloudflare(deployment())
        cloudflare.current["old"]["environment"] = "preview"
        with self.assertRaisesRegex(cleanup.Invalid, "changed identity"):
            self.sweep(cloudflare)
        self.assertEqual(self.deleted(cloudflare), [])

    def test_delete_errors_stop_the_sweep_and_remain_visible(self):
        cloudflare = FakeCloudflare(deployment("one"), deployment("two"))
        cloudflare.fail_delete = True
        with self.assertRaisesRegex(cleanup.Invalid, "Deletion failed"):
            self.sweep(cloudflare)
        self.assertEqual(self.deleted(cloudflare), [("one", False)])


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.api = cleanup.Cloudflare(ACCOUNT_ID, "cloudflare-token")

    def test_snapshot_collects_all_pages_before_returning(self):
        items = [deployment(f"item-{number}") for number in range(26)]
        with patch.object(
            self.api,
            "request",
            side_effect=[page(items[:25], total=26), page(items[25:], 2, 26)],
        ) as request:
            self.assertEqual(len(self.api.deployments(PROJECT_ID)), 26)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            ["/deployments?per_page=25&page=1", "/deployments?per_page=25&page=2"],
        )

    def test_empty_snapshot_accepts_zero_or_one_total_pages(self):
        for total_pages in (0, 1):
            result = page([])
            result["result_info"]["total_pages"] = total_pages
            with patch.object(self.api, "request", return_value=result):
                self.assertEqual(self.api.deployments(PROJECT_ID), {})

    def test_second_page_failure_never_returns_partial_snapshot(self):
        items = [deployment(f"item-{number}") for number in range(25)]
        with patch.object(
            self.api,
            "request",
            side_effect=[page(items, total=26), cleanup.Invalid("Listing failed.")],
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "Listing failed"):
                self.api.deployments(PROJECT_ID)

    def test_invalid_or_changing_pagination_fails_closed(self):
        for field, value in (
            ("page", 2),
            ("per_page", 100),
            ("count", 2),
            ("count", True),
            ("total_count", 26),
            ("total_count", -1),
            ("total_pages", 2),
        ):
            with self.subTest(field=field, value=value):
                result = page([deployment()])
                result["result_info"][field] = value
                with patch.object(self.api, "request", return_value=result):
                    with self.assertRaises(cleanup.Invalid):
                        self.api.deployments(PROJECT_ID)
        items = [deployment(f"item-{number}") for number in range(25)]
        with patch.object(
            self.api,
            "request",
            side_effect=[page(items, total=26), page([deployment()], 2, 27)],
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "pagination changed"):
                self.api.deployments(PROJECT_ID)

    def test_duplicate_ids_and_pagination_limit_fail_closed(self):
        with patch.object(self.api, "request", return_value=page([deployment()] * 2)):
            with self.assertRaisesRegex(cleanup.Invalid, "Duplicate"):
                self.api.deployments(PROJECT_ID)
        items = [deployment(f"item-{number}") for number in range(25)]
        with (
            patch.object(cleanup, "MAX_PAGES", 1),
            patch.object(self.api, "request", return_value=page(items, total=26)),
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "safety limit"):
                self.api.deployments(PROJECT_ID)

    def test_project_requires_current_successful_production_identity(self):
        valid = dict(
            id=PROJECT_ID,
            name="msgspec",
            production_branch="cf-pages",
            canonical_deployment=deployment("live"),
        )
        for change in (
            {"name": "other"},
            {"production_branch": "main"},
            {"canonical_deployment": None},
            {"canonical_deployment": preview()},
            {"canonical_deployment": dict(deployment(), latest_stage={})},
        ):
            with self.subTest(change=change):
                with patch.object(
                    self.api, "request", return_value={"result": dict(valid, **change)}
                ):
                    with self.assertRaises(cleanup.Invalid):
                        self.api.project()

    def test_malformed_deployments_fail_closed(self):
        for change in (
            {"id": "../other"},
            {"project_name": "other"},
            {"project_id": "other"},
            {"environment": "unknown"},
            {"deployment_trigger": None},
            {"latest_stage": None},
            {"latest_stage": {"name": "deploy", "status": "unknown"}},
            {"aliases": "https://pr-42.msgspec.pages.dev"},
            {"aliases": [False]},
            {"aliases": ["duplicate", "duplicate"]},
        ):
            with self.subTest(change=change):
                with self.assertRaises(cleanup.Invalid):
                    cleanup.deployment(dict(deployment(), **change), PROJECT_ID)
        item = deployment()
        del item["aliases"]
        with self.assertRaises(cleanup.Invalid):
            cleanup.deployment(item, PROJECT_ID)

    def test_null_and_empty_aliases_are_valid(self):
        for aliases in (None, []):
            cleanup.deployment(dict(preview(), aliases=aliases), PROJECT_ID)

    def test_malformed_alias_urls_fail_closed(self):
        for alias in (
            "not-a-url",
            "http://old.msgspec.pages.dev",
            "https://user@old.msgspec.pages.dev",
            "https://old.msgspec.pages.dev/path",
            "https://old.msgspec.pages.dev?pr=42",
            "https://old.msgspec.pages.dev#pr-42",
            "https://[",
        ):
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(cleanup.Invalid, "alias URL"):
                    cleanup.deployment(
                        dict(preview(branch="old"), aliases=[alias]), PROJECT_ID
                    )

    def test_mismatched_pr_branch_and_alias_fail_closed(self):
        for item in (
            preview(branch="legacy"),
            preview(),
        ):
            item["aliases"] = ["https://pr-43.msgspec.pages.dev"]
            with self.assertRaisesRegex(cleanup.Invalid, "ambiguous"):
                cleanup.deployment(item, PROJECT_ID)

    def test_only_fixed_project_host_and_validated_ids_receive_credentials(self):
        with patch.object(cleanup, "request_json", return_value=page([])) as request:
            self.api.delete("old", False)
            self.api.delete("closed", True)
        self.assertEqual(
            [call.args for call in request.call_args_list],
            [
                (self.api.prefix + "/deployments/old", "cloudflare-token", "DELETE"),
                (
                    self.api.prefix + "/deployments/closed?force=true",
                    "cloudflare-token",
                    "DELETE",
                ),
            ],
        )
        self.assertEqual(
            self.api.prefix,
            "https://api.cloudflare.com/client/v4/accounts/"
            + ACCOUNT_ID
            + "/pages/projects/msgspec",
        )
        for account in (None, "", "../other", "https://evil.example", "a" * 31):
            with self.assertRaises(cleanup.Invalid):
                cleanup.Cloudflare(account, "token")
        with self.assertRaises(cleanup.Invalid):
            self.api.delete("../other", False)

    def test_unsuccessful_or_malformed_envelopes_fail_closed(self):
        for response in (
            [],
            {},
            dict(page([]), success=False),
            dict(page([]), errors=[{"code": 1}]),
            dict(page([]), errors=None),
        ):
            with patch.object(cleanup, "request_json", return_value=response):
                with self.assertRaises(cleanup.Invalid):
                    self.api.request()


class SourceTests(unittest.TestCase):
    def source(self):
        with patch.object(
            cleanup,
            "request_json",
            return_value={"full_name": cleanup.SOURCE, "id": 10},
        ):
            return cleanup.Source("source-reader-token")

    def test_open_main_prs_paginate_to_completion(self):
        api = self.source()
        prs = [pull_request(number) for number in range(1, 102)]
        with patch.object(
            cleanup, "request_json", side_effect=[prs[:100], prs[100:]]
        ) as request:
            self.assertEqual(api.open_prs(), set(range(1, 102)))
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertTrue(call.args[0].startswith(api.prefix + "/pulls?"))
            self.assertIn("state=open&base=main", call.args[0])
            self.assertIn("per_page=100", call.args[0])
            self.assertEqual(call.args[1], "source-reader-token")
            self.assertTrue(call.kwargs["github"])

    def test_no_open_prs_is_authoritative_without_treating_404_as_closed(self):
        api = self.source()
        with patch.object(cleanup, "request_json", return_value=[]):
            self.assertEqual(api.open_prs(), set())
        with patch.object(
            cleanup, "request_json", side_effect=cleanup.Invalid("HTTP 404")
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "HTTP 404"):
                api.open_prs()

    def test_wrong_source_repository_fails_closed(self):
        for result in ({}, [], {"full_name": "other/repo", "id": 10}):
            with patch.object(cleanup, "request_json", return_value=result):
                with self.assertRaises(cleanup.Invalid):
                    cleanup.Source("token")

    def test_wrong_or_ambiguous_pr_identity_fails_closed(self):
        for path, value in (
            ("number", True),
            ("number", 0),
            ("state", "closed"),
            ("merged_at", "2026-10-20T10:00:00Z"),
            ("base", None),
            ("base.ref", "release"),
            ("base.repo.id", 99),
            ("base.repo.full_name", "other/repo"),
        ):
            with self.subTest(path=path, value=value):
                api = self.source()
                pr = pull_request()
                target = pr
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                with patch.object(cleanup, "request_json", return_value=[pr]):
                    with self.assertRaises(cleanup.Invalid):
                        api.open_prs()
        api = self.source()
        for result in ({}, [None], [pull_request(), pull_request()]):
            with patch.object(cleanup, "request_json", return_value=result):
                with self.assertRaises(cleanup.Invalid):
                    api.open_prs()

    def test_late_page_error_and_limit_fail_instead_of_returning_partial_state(self):
        api = self.source()
        prs = [pull_request(number) for number in range(1, 101)]
        with patch.object(
            cleanup,
            "request_json",
            side_effect=[prs, cleanup.Invalid("Authentication failed.")],
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "Authentication"):
                api.open_prs()
        with (
            patch.object(cleanup, "MAX_PAGES", 1),
            patch.object(cleanup, "request_json", return_value=prs),
        ):
            with self.assertRaisesRegex(cleanup.Invalid, "safety limit"):
                api.open_prs()


class TransportTests(unittest.TestCase):
    def test_redirects_and_http_errors_never_become_absence(self):
        for status in (301, 302, 307, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                error = HTTPError("https://api.example", status, "failure", {}, None)
                with patch.object(cleanup, "build_opener") as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaisesRegex(cleanup.Invalid, f"HTTP {status}"):
                        cleanup.request_json("https://api.example", "secret")
                    self.assertEqual(opener.return_value.open.call_count, 1)
                    self.assertIsInstance(opener.call_args.args[0], cleanup.NoRedirect)
        self.assertIsNone(
            cleanup.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil"
            )
        )

    def test_network_errors_and_invalid_json_fail_closed(self):
        with patch.object(cleanup, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("unreachable")
            with self.assertRaises(cleanup.Invalid):
                cleanup.request_json("https://api.example", "secret")
        with patch.object(cleanup, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"not-json"
            with self.assertRaises(cleanup.Invalid):
                cleanup.request_json("https://api.example", "secret")

    def test_valid_response_and_scoped_authorization_headers(self):
        with patch.object(cleanup, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps({"ok": True})
            self.assertEqual(
                cleanup.request_json("https://api.github.com", "reader", github=True),
                {"ok": True},
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer reader")
            self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")
        for token in (None, ""):
            with self.assertRaises(cleanup.Invalid):
                cleanup.request_json("https://api.example", token)

    def test_main_requires_exact_publisher_main_and_no_inputs(self):
        for repository, ref, argv in (
            (cleanup.SOURCE, "refs/heads/main", ["cleanup.py"]),
            (cleanup.PUBLISHER, "refs/heads/other", ["cleanup.py"]),
            (cleanup.PUBLISHER, "refs/heads/main", ["cleanup.py", "--project=other"]),
        ):
            with (
                patch.dict(os.environ, GITHUB_REPOSITORY=repository, GITHUB_REF=ref),
                patch.object(cleanup.sys, "argv", argv),
                patch.object(cleanup, "sweep") as sweep,
            ):
                with self.assertRaises(cleanup.Invalid):
                    cleanup.main()
                sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
