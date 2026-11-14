---
name: ship
description: Take a change from branch to merged PR or to a release in this fork — format, targeted tests, commit, PR, CI watch, merge, and tag only on a green master. Use when the user says to commit, open or update a PR, merge, release, tag, or runs /ship.
---

# Shipping

This fork owns its CI and releases. Always pass `--repo bill-ramos-rmoswi/data-diff` to `gh`, and never target `datafold/data-diff`. Commit, push, open a PR, merge or tag only when the user asked for that step. Approval for one step doesn't cover the next.

## PR

1. If you're on `master`, create a branch: `feature/`, `fix/`, `ci/`, `docs/`, `perf/` or `chore/`.
2. Run `poetry run ruff format .`, the only lint step.
3. Run `/test` for the change, and say which databases weren't tested.
4. Commit with a message saying what changed and why, ending with the attribution line.
5. **If this change will be released,** bump the version in the same PR, not in a later one. For v0.15.0, a separate bump PR cost three more CI cycles and three hours. The PR includes:
   - `pyproject.toml`, `data_diff/version.py` and the README install URL, all at the same version
   - the CHANGELOG section for it, as `docs-writer` describes

   **SemVer:** a changed default, result order or public behavior makes it minor. Fixes and speed-ups that change nothing visible make it a patch. A checksum value change is minor, with an upgrade note, and must change on every dialect in the same release. The `wheel` job fails the PR if the CHANGELOG has no section for the version.
6. Push, then `gh pr create --repo bill-ramos-rmoswi/data-diff --body-file <file>`. Write the body to the scratchpad first. It covers what changed, the results with counts, what wasn't tested, and fork PRs skipping ASE if relevant.
7. Watch CI in the background: `gh run watch <id> --repo bill-ramos-rmoswi/data-diff --exit-status`. Find the id with `gh run list --repo ... --branch <branch> --limit 1 --json databaseId -q '.[0].databaseId'`.
8. On a failure, use `/ci-triage`. Don't read raw logs in this conversation.

## Merge

Merge only when the user asks (or an orchestrated run's release STOP said so), and only after CI passes on the final commit: `gh pr merge <n> --repo ... --merge --delete-branch`, which also deletes the remote branch. Then switch to `master`, `git pull --ff-only`, and delete the local branch.

## Release

Pushing a `v*` tag runs `release.yml`, which publishes the wheel to a GitHub Release.

1. The version in `pyproject.toml` and `data_diff/version.py` must match the tag, and `python3 dev/release/notes.py <version>` must print its CHANGELOG section. If either fails, the bump belongs in a PR first.
2. **Master CI must be green on the commit being tagged.** Check with `gh run list --repo ... --branch master --limit 1`. If it's red or running, stop and say so. v0.14.0 was tagged on a red master and its release run failed.
3. `git tag v<version> && git push origin v<version>`, then watch `release.yml` in the background.
4. **Check what was published.**
   - `gh release view v<version> --repo ... --json assets -q '.assets[].name'` lists the wheel, the sdist, `SHA256SUMS` and `sbom.spdx.json`.
   - Download them to the scratchpad with `gh release download v<version> --repo ... -D <dir>`, then run `cd <dir> && sha256sum -c SHA256SUMS`.
   - `gh attestation verify <dir>/<wheel> --repo bill-ramos-rmoswi/data-diff` must pass.
   - `python3 dev/release/smoke_wheel.py <dir>/<wheel>` checks the wheel as users will get it.

   Report the release URL and the result of each check.

## gh pitfalls already hit

- Older `gh` versions have no `gh run list --commit` ("unknown flag"). Filter with `--json headSha` instead, which works on every version.
- `gh pr edit` can fail with "Projects (classic) is being deprecated". Use `gh api -X PATCH repos/bill-ramos-rmoswi/data-diff/pulls/<n> -F body=@<file>`.
- Never read tokens from `git credential` or the environment. `gh` is already logged in.
- Don't `sleep` to wait. Use `gh run watch` in the background, or Monitor.

## Examples

- `/ship Open the PR for feature/lob-digest-rowhash.`
- `/ship merge PR 5 once CI is green`
- `/ship release 0.14.0`
