"""Remove obsolete Pages deployments using trusted, current provider metadata."""

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCE = "msgspec/msgspec"
PUBLISHER = "msgspec/docs-deploy"
PROJECT = "msgspec"
MAX_PAGES = 1000
PER_PAGE = 25
PR_BRANCH = re.compile(r"pr-([1-9][0-9]*)\Z")
PR_ALIAS = re.compile(r"https://pr-([1-9][0-9]*)\.msgspec\.pages\.dev\Z")


class Invalid(ValueError):
    """A provider response or trusted configuration violated an invariant."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def identifier(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value),
        "The provider returned an invalid identifier.",
    )
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, token, method="GET", github=False):
    require(isinstance(token, str) and bool(token), "An API token is required.")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "msgspec-docs-cleanup",
    }
    if github:
        headers.update(
            Accept="application/vnd.github+json",
            **{"X-GitHub-Api-Version": "2022-11-28"},
        )
    try:
        with build_opener(NoRedirect()).open(
            Request(url, headers=headers, method=method), timeout=60
        ) as response:
            require(response.status == 200, "The API returned an unexpected status.")
            return json.load(response)
    except HTTPError as exc:
        raise Invalid(f"The {method} API request failed with HTTP {exc.code}.") from exc
    except (URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Invalid("The API request failed or returned invalid JSON.") from exc


class Source:
    def __init__(self, token):
        self.token = token
        self.prefix = f"https://api.github.com/repos/{SOURCE}"
        repo = request_json(self.prefix, self.token, github=True)
        require(
            isinstance(repo, dict)
            and repo.get("full_name") == SOURCE
            and type(repo.get("id")) is int
            and repo["id"] > 0,
            "GitHub returned an unexpected source repository.",
        )
        self.repo_id = repo["id"]

    def open_prs(self):
        numbers = set()
        for page in range(1, MAX_PAGES + 1):
            query = urlencode(
                dict(
                    state="open",
                    base="main",
                    sort="created",
                    direction="asc",
                    per_page=100,
                    page=page,
                )
            )
            items = request_json(
                self.prefix + "/pulls?" + query, self.token, github=True
            )
            require(
                isinstance(items, list) and len(items) <= 100,
                "GitHub returned an invalid pull request page.",
            )
            for pr in items:
                require(
                    isinstance(pr, dict), "GitHub returned an invalid pull request."
                )
                number = pr.get("number")
                base = pr.get("base")
                require(
                    type(number) is int
                    and number > 0
                    and number not in numbers
                    and pr.get("state") == "open"
                    and "merged_at" in pr
                    and pr["merged_at"] is None
                    and isinstance(base, dict)
                    and base.get("ref") == "main"
                    and isinstance(base.get("repo"), dict)
                    and base["repo"].get("id") == self.repo_id
                    and base["repo"].get("full_name") == SOURCE,
                    "GitHub returned an ambiguous or unexpected pull request identity.",
                )
                numbers.add(number)
            if len(items) < 100:
                return numbers
        raise Invalid("GitHub pagination exceeded the safety limit.")


class Cloudflare:
    def __init__(self, account, token):
        require(
            isinstance(account, str) and re.fullmatch(r"[0-9a-f]{32}", account),
            "CF_ACCOUNT_ID must be the trusted Cloudflare account ID.",
        )
        self.token = token
        self.prefix = (
            "https://api.cloudflare.com/client/v4/accounts/"
            + account
            + "/pages/projects/"
            + PROJECT
        )

    def request(self, suffix="", method="GET"):
        result = request_json(self.prefix + suffix, self.token, method)
        require(
            isinstance(result, dict)
            and result.get("success") is True
            and result.get("errors") == []
            and "result" in result,
            "Cloudflare returned an unsuccessful or malformed API response.",
        )
        return result

    def project(self):
        project = self.request()["result"]
        require(
            isinstance(project, dict)
            and project.get("name") == PROJECT
            and project.get("production_branch") == "cf-pages",
            "Cloudflare returned an unexpected project or production branch.",
        )
        identifier(project.get("id"))
        current = deployment(project.get("canonical_deployment"), project["id"])
        require(
            current["environment"] == "production"
            and current["latest_stage"]["name"] == "deploy"
            and current["latest_stage"]["status"] == "success",
            "Cloudflare did not identify a successful canonical production deployment.",
        )
        return project["id"], current["id"]

    def deployments(self, project_id):
        snapshot = {}
        expected_total = None
        for page in range(1, MAX_PAGES + 1):
            result = self.request(
                "/deployments?" + urlencode(dict(per_page=PER_PAGE, page=page))
            )
            items, info = result["result"], result.get("result_info")
            require(
                isinstance(items, list)
                and len(items) <= PER_PAGE
                and isinstance(info, dict)
                and all(
                    type(info.get(key)) is int
                    for key in (
                        "page",
                        "per_page",
                        "count",
                        "total_count",
                        "total_pages",
                    )
                )
                and info["page"] == page
                and info["per_page"] == PER_PAGE
                and info["count"] == len(items)
                and info["total_count"] >= 0
                and info["total_pages"]
                in {
                    (info["total_count"] + PER_PAGE - 1) // PER_PAGE,
                    max(1, (info["total_count"] + PER_PAGE - 1) // PER_PAGE),
                },
                "Cloudflare returned inconsistent deployment pagination.",
            )
            if expected_total is None:
                expected_total = info["total_count"]
            require(
                info["total_count"] == expected_total,
                "Cloudflare deployment pagination changed; retry the sweep.",
            )
            for item in items:
                item = deployment(item, project_id)
                require(
                    item["id"] not in snapshot, "Duplicate deployment in pagination."
                )
                snapshot[item["id"]] = item
            if page >= info["total_pages"]:
                require(
                    len(snapshot) == expected_total,
                    "Cloudflare returned an incomplete deployment snapshot.",
                )
                return snapshot
            require(len(items) == PER_PAGE, "Cloudflare returned an incomplete page.")
        raise Invalid("Cloudflare pagination exceeded the safety limit.")

    def get(self, deployment_id, project_id):
        item = deployment(
            self.request("/deployments/" + identifier(deployment_id))["result"],
            project_id,
        )
        require(item["id"] == deployment_id, "Cloudflare returned another deployment.")
        return item

    def delete(self, deployment_id, force):
        suffix = "/deployments/" + identifier(deployment_id)
        if force:
            suffix += "?force=true"
        self.request(suffix, "DELETE")


def deployment(item, project_id):
    require(isinstance(item, dict), "Cloudflare returned an invalid deployment.")
    identifier(item.get("id"))
    trigger = item.get("deployment_trigger")
    metadata = trigger.get("metadata") if isinstance(trigger, dict) else None
    stage = item.get("latest_stage")
    require(
        item.get("project_name") == PROJECT
        and item.get("project_id") == project_id
        and item.get("environment") in ("preview", "production")
        and isinstance(metadata, dict)
        and isinstance(metadata.get("branch"), str)
        and bool(metadata["branch"])
        and isinstance(stage, dict)
        and stage.get("name")
        in ("queued", "initialize", "clone_repo", "build", "deploy")
        and stage.get("status")
        in ("idle", "active", "success", "failure", "canceled", "skipped")
        and "aliases" in item,
        "Cloudflare returned an unexpected deployment identity or stage.",
    )
    aliases = item["aliases"]
    require(
        aliases is None
        or (
            isinstance(aliases, list)
            and all(isinstance(alias, str) and bool(alias) for alias in aliases)
            and len(set(aliases)) == len(aliases)
        ),
        "Cloudflare returned invalid deployment aliases.",
    )
    branch = metadata["branch"]
    for alias in aliases or []:
        try:
            parsed = urlsplit(alias)
        except ValueError as exc:
            raise Invalid(
                "Cloudflare returned an invalid deployment alias URL."
            ) from exc
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.netloc == parsed.hostname
            and not parsed.path
            and not parsed.query
            and not parsed.fragment,
            "Cloudflare returned an invalid deployment alias URL.",
        )
    if item["environment"] == "preview":
        expected = f"https://{branch}.msgspec.pages.dev"
        for alias in aliases or []:
            require(
                (not PR_BRANCH.fullmatch(branch) or alias == expected)
                and (not PR_ALIAS.fullmatch(alias) or alias == expected),
                "The preview branch and pull request alias have ambiguous identities.",
            )
    return item


def terminal(item):
    stage = item["latest_stage"]
    return stage["status"] in ("failure", "canceled", "skipped") or (
        stage["name"] == "deploy" and stage["status"] == "success"
    )


def serving_open_pr(item, open_prs):
    branch = item["deployment_trigger"]["metadata"]["branch"]
    match = PR_BRANCH.fullmatch(branch)
    return (
        item["environment"] == "preview"
        and match is not None
        and int(match[1]) in open_prs
        and f"https://{branch}.msgspec.pages.dev" in (item["aliases"] or [])
    )


def sweep(cloudflare, source):
    project_id, canonical = cloudflare.project()
    snapshot = cloudflare.deployments(project_id)
    require(canonical in snapshot, "The snapshot omitted canonical production.")
    open_prs = source.open_prs()
    aliases = set()
    for item in snapshot.values():
        for alias in item["aliases"] or []:
            require(alias not in aliases, "Several deployments claim the same alias.")
            aliases.add(alias)
    deleted = 0
    for original in snapshot.values():
        if (
            original["id"] == canonical
            or not terminal(original)
            or serving_open_pr(original, open_prs)
        ):
            continue
        item = cloudflare.get(original["id"], project_id)
        require(
            item["environment"] == original["environment"]
            and item["deployment_trigger"]["metadata"]["branch"]
            == original["deployment_trigger"]["metadata"]["branch"],
            "A deployment changed identity during cleanup.",
        )
        if not terminal(item):
            continue
        force = item["environment"] == "preview" and bool(item["aliases"])
        if force:
            open_prs = source.open_prs()
        if serving_open_pr(item, open_prs):
            continue
        current_project_id, current_canonical = cloudflare.project()
        require(current_project_id == project_id, "The Cloudflare project changed.")
        if item["id"] == current_canonical:
            continue
        cloudflare.delete(item["id"], force)
        deleted += 1
        print("Deleted deployment " + item["id"] + ".", flush=True)
    result = dict(
        listed=len(snapshot), deleted=deleted, retained=len(snapshot) - deleted
    )
    print(json.dumps(result, sort_keys=True))
    return result


def main():
    require(len(sys.argv) == 1, "Cleanup accepts no arguments.")
    require(
        os.environ.get("GITHUB_REPOSITORY") == PUBLISHER
        and os.environ.get("GITHUB_REF") == "refs/heads/main",
        "Cleanup requires the trusted publisher main branch.",
    )
    sweep(
        Cloudflare(os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_PAGES_TOKEN")),
        Source(os.environ.get("SOURCE_TOKEN")),
    )


if __name__ == "__main__":
    try:
        main()
    except Invalid as exc:
        sys.exit(str(exc))
