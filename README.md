# msgspec documentation publisher

This repository holds the Cloudflare authority for documentation built by `msgspec/msgspec`. The source repository builds and checks documentation without deployment credentials. A Rust Cloudflare Worker runs the publisher GitHub App, requests publication by identifying one successful source workflow run and its latest attempt, and reports the required `Docs preview` check. The publisher independently resolves the source, pull request, artifact, and destination through the GitHub API.

## Setup

Create this repository as public `msgspec/docs-deploy`, with `main` as its default branch. Protect `main` and limit permission to change its code to trusted maintainers. Seed a separate `cf-pages` branch with the existing production snapshot. Treat that branch as static data; it must not contain GitHub workflow files. Keep the publisher workflow and Python helpers only on `main`.

Configure a GitHub App installed only on `msgspec/msgspec` and `msgspec/docs-deploy`. Its maximum repository permissions are Actions write, Checks write, Contents read, Pull requests read, and the required Metadata read permission. Do not grant Contents write, Workflows write, Secrets, Administration, or other permissions. Keep the App's private key only in the Worker and this trusted publisher; never store it in source CI. The Worker dispatches publication and reports its check after validating the result. The publisher narrows each minted token to the source repository and Actions read, Contents read, and Pull requests read. This token cannot write publisher code or source code. Only the production job's own `GITHUB_TOKEN` can write the publisher snapshot.

Set repository variable `DOCS_APP_ID` and repository secret `DOCS_APP_PRIVATE_KEY`. Create environments `docs-preview` and `docs-live`, and restrict both environments to deployments from `main`. In each environment, set secrets `CF_ACCOUNT_ID` and `CF_PAGES_TOKEN`. Scope the Cloudflare token to the intended account and Pages publishing capability. Confirm that the existing Pages project is `msgspec`, its production branch is `cf-pages`, and its production domain is `https://msgspec.dev`.

The source integration must invoke `.github/workflows/docs-pr.yml` for pull requests and pushes to `main`. It must upload exactly one artifact named `docs-site`, with a root `index.html`, after a successful build. Use artifact overwrite on full reruns so that the latest build replaces earlier data. A rerun of failed link checks may reuse a build artifact from an earlier attempt of the same run and SHA, provided the latest attempt succeeds. Keep the source artifact's retention at one day. Missing or expired artifacts cause an actionable failure; the publisher never selects another source run as a fallback.

## Dispatch and result contract

Dispatch `.github/workflows/publish.yml` on `main` with exactly these required string inputs:

```json
{
  "source-run-id": "123456789",
  "source-run-attempt": "2"
}
```

Both values must be positive decimal integers. The publisher accepts no caller-provided repository, ref, pull request number, destination, project, account, script, or URL. It looks up the source workflow ID by its fixed path, requires the run and the requested attempt to have succeeded, and requires that attempt to remain the run's latest attempt.

For a `pull_request` run, the publisher requires exactly one open pull request targeting the source repository's `main` branch, with the exact head repository ID, branch, and SHA reported by the source run. When GitHub omits fork pull requests from the run response, the publisher resolves commit associations or paginates open pull requests for the head owner and branch. Ambiguous, closed, missing, and stale heads fail validation. The destination branch is `pr-N`, and the returned URL is `https://pr-N.msgspec.pages.dev`.

For a `push` run, the publisher requires `main` in the source repository. The destination branch is always `cf-pages`. Immediately before upload, it checks the latest attempt and current source head again. An outdated source `main` head or a byte-identical production snapshot produces a successful result with `published: false`. Preview runs cancel superseded publication jobs for the same pull request. A single production concurrency group covers deployment and snapshot publication together, with cancellation disabled.

Every successful publication job uploads artifact `docs-deployment` containing only `result.json`, retained for seven days. The Worker must bind the result to its expected source run, attempt, SHA, publisher run, target, publication flag, and exact URL before reporting success. A result has this schema:

```json
{
  "source_run_id": 123456789,
  "source_run_attempt": 2,
  "source_sha": "0123456789abcdef0123456789abcdef01234567",
  "target": "preview",
  "publisher_run_id": 987654321,
  "url": "https://pr-42.msgspec.pages.dev",
  "published": true
}
```

The numeric fields are JSON integers. `target` is `preview` or `production`; production always returns `https://msgspec.dev`. No result artifact is emitted when validation, deployment, or snapshot publication fails. A failed snapshot push therefore leaves the workflow failed even if the Cloudflare upload already succeeded. A rerun can publish again and repair the snapshot.

## Artifact and credential boundaries

The publisher downloads the ZIP by the exact artifact ID returned for the verified source run and SHA. It requires GitHub's SHA-256 digest and verifies the archive bytes before extraction. The artifact must have been created no earlier than the original source run's creation, allowing successful link-only reruns to reuse that run's existing build. The publisher still requires the latest attempt to have succeeded and verifies the artifact's exact run ID, repository ID, head repository ID, branch, and SHA. Signed archive downloads do not receive the GitHub API token.

Artifacts extract only into a fresh `$RUNNER_TEMP/docs-data/site` directory, outside the trusted publisher checkout. Validation rejects traversal, absolute and drive paths, backslashes, duplicate and conflicting paths, symbolic links, special files, encrypted entries, Git metadata, root `.github` entries, root `_worker.js` files or directories, and root `functions` entries. A root `index.html` is required. Resource limits are 512 MiB for the ZIP, 1 GiB for extracted files, and 100,000 entries.

The workflow never checks out or executes source code, artifact scripts, or artifact Wrangler configuration. Pinned Wrangler runs from the trusted publisher checkout with a fixed project and validated branch and commit arguments. Repository code must not add a Wrangler configuration that changes this boundary. Cloudflare credentials are supplied only to the deployment action.

Production checks out `cf-pages` only as snapshot data. Snapshot creation uses Git object and tree plumbing with content filters disabled and no commit hooks. This preserves exact file bytes, including files that an artifact `.gitignore` would otherwise hide. Snapshot commits retain GitHub Actions bot attribution. Their push uses only the publisher job's `GITHUB_TOKEN`, never an App token or a source write token. A normal push rejects unexpected concurrent snapshot changes.

## Development

The helpers and tests use Python 3.12 and its standard library. The CI workflow runs API, routing, archive, result, Git snapshot, and workflow policy fixtures without credentials or network access.

```text
python -m unittest discover -s tests -v
actionlint -no-color .github/workflows/publish.yml .github/workflows/ci.yml
zizmor --no-progress --format plain .github/workflows
```

Tests cover malformed identities, stale attempts and heads, wrong repositories and workflows, fork association fallbacks and ambiguity, missing and expired artifacts, link-only reruns reusing the same run's build, artifacts predating the original run, digest mismatch, unsafe ZIP entries, production skip behavior, exact preview aliases, and snapshot filters and attribution. Action SHAs and Wrangler are pinned; update them deliberately and run all checks when changing the trust boundary.

Local tests do not exercise live GitHub App installation, environment restrictions, cross-repository dispatch, artifact service redirects, Cloudflare credentials, or actual Pages upload. Validate those integrations after provisioning with a current successful source build and retain its publisher result as evidence.
