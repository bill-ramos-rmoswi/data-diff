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
5. Push, then `gh pr create --repo bill-ramos-rmoswi/data-diff --body-file <file>`. Write the body to the scratchpad first. It covers what changed, the results with counts, what wasn't tested, and fork PRs skipping ASE if relevant.
6. Watch CI in the background: `gh run watch <id> --repo bill-ramos-rmoswi/data-diff --exit-status`. Find the id with `gh run list --repo ... --branch <branch> --limit 1 --json databaseId -q '.[0].databaseId'`.
7. On a failure, use `/ci-triage`. Don't read raw logs in this conversation.

## Merge

Merge only when the user asks, and only after CI passes on the final commit: `gh pr merge <n> --repo ... --merge`. Then switch to `master`, `git pull --ff-only`, and delete the branch.

## Release

Pushing a `v*` tag runs `release.yml`, which publishes the wheel to a GitHub Release.

1. The version in `pyproject.toml` and `data_diff/version.py` must match the tag.
2. **Master CI must be green on the commit being tagged.** Check with `gh run list --repo ... --branch master --limit 1`. If it's red or running, stop and say so. v0.14.0 was tagged on a red master and its release run failed.
3. `git tag v<version> && git push origin v<version>`, then watch `release.yml` in the background and report the release URL from `gh release view v<version> --repo ...`.

## gh pitfalls already hit

- Older `gh` versions have no `gh run list --commit` ("unknown flag"). Filter with `--json headSha` instead, which works on every version.
- `gh pr edit` can fail with "Projects (classic) is being deprecated". Use `gh api -X PATCH repos/bill-ramos-rmoswi/data-diff/pulls/<n> -F body=@<file>`.
- Never read tokens from `git credential` or the environment. `gh` is already logged in.
- Don't `sleep` to wait. Use `gh run watch` in the background, or Monitor.

## Examples

- `/ship Open the PR for feature/lob-digest-rowhash.`
- `/ship merge PR 5 once CI is green`
- `/ship release 0.14.0`
