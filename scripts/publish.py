"""Validate source metadata and treat documentation artifacts strictly as data."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SOURCE = "msgspec/msgspec"
PUBLISHER = "msgspec/docs-deploy"
WORKFLOW = ".github/workflows/docs-pr.yml"
PRODUCTION_URL = "https://msgspec.dev"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 100_000


class Invalid(ValueError):
    """An input or source invariant failed."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def positive(value):
    require(
        re.fullmatch(r"[1-9][0-9]*", str(value)) is not None,
        "Run IDs and attempts must be positive decimal integers.",
    )
    return int(value)


def timestamp(value):
    try:
        require(isinstance(value, str), "GitHub returned an invalid timestamp.")
        result = datetime.fromisoformat(value)
        require(
            result.tzinfo is not None,
            "GitHub returned a timestamp without a time zone.",
        )
        return result
    except (ValueError, AttributeError) as exc:
        raise Invalid("GitHub returned an invalid timestamp.") from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        require(bool(token), "SOURCE_TOKEN is required.")
        self.token = token

    def request(self, path):
        prefix = f"/repos/{SOURCE}"
        require(
            path == prefix or path.startswith(prefix + "/"),
            "Unexpected API repository.",
        )
        return Request(
            "https://api.github.com" + path,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "msgspec-docs-publisher",
            },
        )

    def get(self, path):
        with urlopen(self.request(path), timeout=60) as response:
            return json.load(response)

    def pages(self, path, key=None, params=None):
        for page in range(1, 1001):
            query = dict(params or {}, per_page=100, page=page)
            result = self.get(path + "?" + urlencode(query))
            items = result[key] if key else result
            require(isinstance(items, list), "GitHub returned an invalid page.")
            yield from items
            if len(items) < 100:
                return
        raise Invalid("GitHub pagination exceeded the safety limit.")

    def download(self, artifact_id, destination):
        # GitHub's signed archive redirect must not receive the API token.
        request = self.request(
            f"/repos/{SOURCE}/actions/artifacts/{positive(artifact_id)}/zip"
        )
        try:
            build_opener(NoRedirect()).open(request, timeout=60)
        except HTTPError as exc:
            require(
                exc.code == 302, "Artifact download failed; rebuild expired artifacts."
            )
            location = exc.headers.get("Location", "")
        else:
            raise Invalid("GitHub did not return an artifact download redirect.")
        parsed = urlsplit(location)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password,
            "GitHub returned an unsafe archive URL.",
        )
        with (
            urlopen(Request(location), timeout=60) as response,
            destination.open("wb") as output,
        ):
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                require(total <= MAX_ARCHIVE_BYTES, "Artifact ZIP exceeds 512 MiB.")
                output.write(chunk)


def verify_run(run, run_id, attempt, repo_id, workflow_id):
    require(run.get("id") == run_id, "Source run ID does not match.")
    require(
        run.get("run_attempt") == attempt,
        "The requested source attempt is stale; dispatch the latest successful attempt.",
    )
    require(
        run.get("repository", {}).get("id") == repo_id,
        "The source run belongs to another repository.",
    )
    require(
        run.get("workflow_id") == workflow_id and run.get("path") == WORKFLOW,
        "The source run is not the expected documentation workflow.",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The source attempt must have completed successfully.",
    )
    require(
        isinstance(run.get("head_sha"), str)
        and SHA.fullmatch(run["head_sha"]) is not None,
        "GitHub returned an invalid source SHA.",
    )


def matches_pr(pr, run, repo_id):
    head = pr.get("head", {})
    base = pr.get("base", {})
    head_repo = head.get("repo") or {}
    run_repo = run.get("head_repository") or {}
    return (
        pr.get("state") == "open"
        and base.get("ref") == "main"
        and (base.get("repo") or {}).get("id") == repo_id
        and head_repo.get("id") == run_repo.get("id")
        and head.get("ref") == run.get("head_branch")
        and head.get("sha") == run["head_sha"]
    )


def resolve_pr(api, run, repo_id):
    prefix = f"/repos/{SOURCE}"
    run_repo = run.get("head_repository") or {}
    require(
        type(run_repo.get("id")) is int and run_repo["id"] > 0,
        "The source head repository is missing.",
    )
    associated = run.get("pull_requests") or []
    if associated:
        candidates = associated
    else:
        candidates = list(api.pages(f"{prefix}/commits/{run['head_sha']}/pulls"))
        if not candidates:
            owner = run_repo.get("owner", {}).get("login", "")
            branch = run.get("head_branch", "")
            require(
                bool(owner) and bool(branch),
                "The source head owner or branch is missing.",
            )
            candidates = list(
                api.pages(
                    f"{prefix}/pulls",
                    params={
                        "state": "open",
                        "base": "main",
                        "head": f"{owner}:{branch}",
                    },
                )
            )
    numbers = {positive(candidate["number"]) for candidate in candidates}
    matching = []
    for number in sorted(numbers):
        pr = api.get(f"{prefix}/pulls/{number}")
        if matches_pr(pr, run, repo_id):
            matching.append(number)
    require(
        len(matching) == 1,
        "The source run must identify exactly one open, current pull request targeting main.",
    )
    return matching[0]


def verify_artifact(artifact, run, repo_id, run_created_at):
    require(
        artifact.get("name") == "docs-site", "Unexpected documentation artifact name."
    )
    require(
        artifact.get("expired") is False,
        "The docs-site artifact expired; rebuild the source workflow and dispatch its latest attempt.",
    )
    positive(artifact.get("id"))
    require(
        isinstance(artifact.get("digest"), str)
        and DIGEST.fullmatch(artifact["digest"]) is not None,
        "The docs-site artifact must provide a SHA-256 digest.",
    )
    linked = artifact.get("workflow_run") or {}
    require(
        linked.get("id") == run["id"]
        and linked.get("repository_id") == repo_id
        and linked.get("head_sha") == run["head_sha"]
        and linked.get("head_repository_id") == run["head_repository"]["id"]
        and linked.get("head_branch") == run["head_branch"],
        "The artifact does not belong to the verified source run and head.",
    )
    require(
        timestamp(artifact.get("created_at")) >= timestamp(run_created_at),
        "The docs-site artifact predates the source run; rebuild the source workflow.",
    )


def resolve(api, run_id, attempt):
    run_id, attempt = positive(run_id), positive(attempt)
    prefix = f"/repos/{SOURCE}"
    repo = api.get(prefix)
    require(
        repo.get("full_name", "").lower() == SOURCE, "Unexpected source repository."
    )
    repo_id = positive(repo.get("id"))
    workflow = api.get(f"{prefix}/actions/workflows/docs-pr.yml")
    require(workflow.get("path") == WORKFLOW, "Unexpected source workflow path.")
    workflow_id = positive(workflow.get("id"))
    latest = api.get(f"{prefix}/actions/runs/{run_id}")
    run = api.get(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}")
    verify_run(latest, run_id, attempt, repo_id, workflow_id)
    verify_run(run, run_id, attempt, repo_id, workflow_id)
    for field in ("head_sha", "head_branch", "event", "head_repository"):
        require(latest.get(field) == run.get(field), "Source attempt metadata changed.")
    if run.get("event") == "pull_request":
        pr = resolve_pr(api, run, repo_id)
        target, branch = "preview", f"pr-{pr}"
    elif run.get("event") == "push":
        require(
            run.get("head_branch") == "main"
            and (run.get("head_repository") or {}).get("id") == repo_id,
            "Production requires a push to the source repository's main branch.",
        )
        pr, target, branch = None, "production", "cf-pages"
    else:
        raise Invalid(
            "Only pull_request and push source events can publish documentation."
        )
    artifacts = list(
        api.pages(
            f"{prefix}/actions/runs/{run_id}/artifacts",
            "artifacts",
            {"name": "docs-site"},
        )
    )
    artifacts = [item for item in artifacts if item.get("name") == "docs-site"]
    require(
        len(artifacts) == 1,
        "Exactly one docs-site artifact is required; rebuild missing or ambiguous artifacts.",
    )
    artifact = artifacts[0]
    verify_artifact(artifact, run, repo_id, latest.get("created_at"))
    return {
        "source_run_id": run_id,
        "source_run_attempt": attempt,
        "source_sha": run["head_sha"],
        "target": target,
        "branch": branch,
        "pr": pr,
        "artifact_id": artifact["id"],
        "artifact_digest": artifact["digest"],
    }


def validate_zip(archive, digest, destination):
    require(
        isinstance(digest, str) and DIGEST.fullmatch(digest) is not None,
        "An artifact SHA-256 digest is required.",
    )
    require(
        archive.stat().st_size <= MAX_ARCHIVE_BYTES, "Artifact ZIP exceeds 512 MiB."
    )
    with archive.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    require("sha256:" + actual == digest, "Artifact SHA-256 digest mismatch.")
    require(
        not destination.exists(), "Artifact extraction requires a fresh data directory."
    )
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        require(
            0 < len(entries) <= MAX_FILES, "Artifact ZIP has an invalid entry count."
        )
        require(
            sum(entry.file_size for entry in entries) <= MAX_EXTRACTED_BYTES,
            "Artifact ZIP expands beyond 1 GiB.",
        )
        names = set()
        files = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            require(
                name
                and name == entry.orig_filename
                and "\\" not in name
                and "\x00" not in entry.orig_filename
                and not path.is_absolute()
                and not PureWindowsPath(name).drive
                and all(
                    part not in ("", ".", "..") for part in name.rstrip("/").split("/")
                )
                and not any(":" in part for part in path.parts),
                "Artifact ZIP contains an unsafe path.",
            )
            require(
                all(part.lower() != ".git" for part in path.parts),
                "Artifact ZIP cannot contain Git metadata.",
            )
            require(
                path.parts[0].lower() != ".github",
                "Artifact ZIP cannot introduce publisher workflow files.",
            )
            require(
                path.parts[0].lower() not in ("_worker.js", "functions"),
                "Artifact ZIP cannot contain Pages Workers or Functions entrypoints.",
            )
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(
                mode in (0, stat.S_IFREG, stat.S_IFDIR),
                "Artifact ZIP cannot contain symbolic links or special files.",
            )
            require(
                (mode != stat.S_IFDIR or entry.is_dir())
                and (mode != stat.S_IFREG or not entry.is_dir()),
                "Artifact ZIP entry types must agree with their paths.",
            )
            require(not entry.flag_bits & 1, "Encrypted ZIP entries are not supported.")
            canonical = str(path)
            require(canonical not in names, "Artifact ZIP contains duplicate paths.")
            names.add(canonical)
            if not entry.is_dir():
                files.add(canonical)
        require(
            "index.html" in files, "Artifact ZIP must contain a root index.html file."
        )
        require(
            not any(
                str(parent) in files
                for name in names
                for parent in PurePosixPath(name).parents
                if str(parent) != "."
            ),
            "Artifact ZIP contains conflicting file and directory paths.",
        )
        destination.mkdir(parents=True)
        for entry in entries:
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def output(values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            require(
                "\n" not in str(value) and "\r" not in str(value),
                "Invalid step output.",
            )
            stream.write(f"{key}={value}\n")


def inventory(directory):
    require(
        directory.is_dir() and not directory.is_symlink(),
        "A real site or snapshot directory is required.",
    )
    result = {}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if relative.parts[0] == ".git":
            continue
        require(not path.is_symlink(), "Snapshot or site contains a symbolic link.")
        if path.is_file():
            with path.open("rb") as source:
                result[relative.as_posix()] = hashlib.file_digest(
                    source, "sha256"
                ).hexdigest()
    return result


def snapshot_commit(site, directory, sha):
    require(SHA.fullmatch(sha) is not None, "Invalid snapshot source SHA.")
    require(
        (directory / ".git").exists(),
        "The seeded cf-pages snapshot checkout is required.",
    )
    inventory(directory)
    paths = sorted(inventory(site))
    git = ["git", "-C", str(directory)]
    parent = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    require(SHA.fullmatch(parent) is not None, "The snapshot parent is invalid.")
    subprocess.run(git + ["read-tree", "--empty"], check=True)
    index = bytearray()
    # Plumbing avoids artifact-controlled attributes, content filters, and hooks.
    for start in range(0, len(paths), 128):
        batch = paths[start : start + 128]
        hashes = subprocess.check_output(
            git
            + ["hash-object", "--no-filters", "-w", "--"]
            + [str(site / path) for path in batch],
            text=True,
        ).splitlines()
        require(
            len(hashes) == len(batch) and all(SHA.fullmatch(value) for value in hashes),
            "Git returned invalid snapshot object hashes.",
        )
        for path, object_id in zip(batch, hashes):
            index.extend(f"100644 {object_id}\t{path}\0".encode())
    subprocess.run(
        git + ["update-index", "-z", "--index-info"], input=index, check=True
    )
    tree = subprocess.check_output(git + ["write-tree"], text=True).strip()
    attribution = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        attribution[f"GIT_{role}_NAME"] = "github-actions[bot]"
        attribution[f"GIT_{role}_EMAIL"] = (
            "41898282+github-actions[bot]@users.noreply.github.com"
        )
    return subprocess.check_output(
        git + ["commit-tree", tree, "-p", parent, "-m", f"deploy: {sha}"],
        env=attribution,
        text=True,
    ).strip()


def snapshot(site, directory, sha):
    commit = snapshot_commit(site, directory, sha)
    require(
        SHA.fullmatch(commit) is not None, "Git returned an invalid snapshot commit."
    )
    token = os.environ["SNAPSHOT_TOKEN"]
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
        }
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "push",
            f"https://github.com/{PUBLISHER}.git",
            f"{commit}:refs/heads/cf-pages",
        ],
        env=env,
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("metadata", "prepare", "revalidate", "snapshot", "result")
    )
    args = parser.parse_args()
    require(
        os.environ.get("GITHUB_REPOSITORY") == PUBLISHER
        and os.environ.get("GITHUB_REF") == "refs/heads/main",
        "The publisher may run only in msgspec/docs-deploy on main.",
    )
    data = Path(os.environ["RUNNER_TEMP"]) / "docs-data"
    site = data / "site"
    plan_path = data / "plan.json"
    if args.command == "result":
        plan = json.loads(plan_path.read_text())
        require(
            os.environ["PUBLISHED"] in ("true", "false"), "Invalid publication result."
        )
        published = os.environ["PUBLISHED"] == "true"
        url = (
            PRODUCTION_URL
            if plan["target"] == "production"
            else os.environ["DEPLOYMENT_URL"]
        )
        if plan["target"] == "preview":
            expected = f"https://{plan['branch']}.msgspec.pages.dev"
            require(
                url in (expected, expected + "/"),
                "Wrangler did not return the expected msgspec preview alias URL.",
            )
            url = expected
        result = {
            key: plan[key]
            for key in ("source_run_id", "source_run_attempt", "source_sha", "target")
        }
        result.update(
            publisher_run_id=positive(os.environ["GITHUB_RUN_ID"]),
            url=url,
            published=published,
        )
        (data / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return
    if args.command == "snapshot":
        plan = json.loads(plan_path.read_text())
        require(
            plan["target"] == "production", "Only production can update the snapshot."
        )
        snapshot(
            site, Path(os.environ["GITHUB_WORKSPACE"]) / "snapshot", plan["source_sha"]
        )
        return
    api = GitHub(os.environ["SOURCE_TOKEN"])
    plan = resolve(api, os.environ["SOURCE_RUN_ID"], os.environ["SOURCE_RUN_ATTEMPT"])
    if args.command == "metadata":
        output({key: plan[key] for key in ("target", "branch", "source_sha")})
    elif args.command == "prepare":
        require(
            plan["target"] == os.environ["EXPECTED_TARGET"]
            and plan["branch"] == os.environ["EXPECTED_BRANCH"]
            and plan["source_sha"] == os.environ["EXPECTED_SHA"],
            "Source routing changed after metadata validation.",
        )
        data.mkdir(exist_ok=False)
        archive = data / "docs-site.zip"
        api.download(plan["artifact_id"], archive)
        validate_zip(archive, plan["artifact_digest"], site)
        archive.unlink()
        plan_path.write_text(json.dumps(plan))
    elif args.command == "revalidate":
        require(
            plan == json.loads(plan_path.read_text()),
            "Source metadata or artifact changed after download; rebuild and retry.",
        )
        publish = True
        if plan["target"] == "production":
            head = api.get(f"/repos/{SOURCE}/git/ref/heads/main")["object"]["sha"]
            if head != plan["source_sha"]:
                print(
                    "Production deployment skipped because the source main branch advanced."
                )
                publish = False
            elif inventory(site) == inventory(
                Path(os.environ["GITHUB_WORKSPACE"]) / "snapshot"
            ):
                print(
                    "Production deployment skipped because the snapshot is identical."
                )
                publish = False
        output({"publish": str(publish).lower()})


if __name__ == "__main__":
    try:
        main()
    except (Invalid, HTTPError, zipfile.BadZipFile) as exc:
        print(f"Publisher validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
