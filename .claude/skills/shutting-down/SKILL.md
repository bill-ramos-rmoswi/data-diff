---
name: shutting-down
description: End-of-session checklist for this repo. Use when the user says they're done for the day, closing the devcontainer, shutting down, or runs /shutting-down. Reports unsaved work, open PRs and running jobs, then says what to stop from WSL.
---

# Shutting down a session

Check what would be lost or left running, report it, and change nothing without asking. Committing, pushing, stashing and stopping containers all need the user's go-ahead.

## 1. Where this session runs

`/.dockerenv` exists in the devcontainer, not in WSL. The devcontainer has no Docker socket, so database commands in step 5 are for the user to run in a WSL terminal. In WSL, you can run them after asking.

## 2. Git

Run these and report anything that isn't clean:

```shell
git status --short --branch
git log --oneline @{upstream}..HEAD 2>/dev/null || echo "branch has no upstream"
git stash list
git branch -vv | grep ': gone]'
```

- Uncommitted or untracked changes: list them.
- If `.devcontainer/devcontainer-lock.json` is modified, VS Code rewrote it after a feature change or a rebuild. Show its diff, and suggest committing it with the `devcontainer.json` change that caused it, or on its own if the features only moved to newer versions.
- Commits that aren't pushed, or a branch with no upstream: say so. They exist only on this machine.
- Stashes: list them, because they're easy to forget.
- Branches whose upstream is gone: their PR was merged or closed, and they can be deleted.
- Untracked `benchmark_*.jsonl` from `dev/perf/run_perf.py`: ask whether to keep them.

## 3. GitHub

Pass `--repo bill-ramos-rmoswi/data-diff`. Skip this step if `gh auth status` fails, and say so.

```shell
gh pr list --repo bill-ramos-rmoswi/data-diff --author @me --state open
gh run list --repo bill-ramos-rmoswi/data-diff --limit 5
```

Report open PRs with failing or running checks, and any workflow run in progress or failed in the last day. A merged PR that should have released needs a pushed `v*` tag matching `version` in `pyproject.toml`, so check `git ls-remote --tags origin` and `gh release list` when the version on master has no release.

## 4. This session

- Background tasks you started (test runs, watches, `run_perf.py`): list any still running, and ask whether to stop them or let them finish.
- Anything learned this session that isn't in the repo or CLAUDE.md and would be needed next time: offer to save it as a memory. Don't save what git history already records.
- Write the handoff as a project memory named `handoff`, replacing the previous one. Its body has what was done, what's in progress (branch, PR, background jobs), the next step as a prompt the user can paste (for example `/perf ...` or the next phase of a one-shot prompt), and the model that step needs. `/hello` reads it, so the next session doesn't start from pasted output. Do this without asking, because it changes nothing in the repo.
- Finish with a handoff of at most five lines: what was done, what's in progress, and the next step. The session is saved in WSL at `~/.claude-devcontainer` (or `~/.claude` in WSL), so `claude --continue` reopens it.

## 5. Databases

The databases keep running when VS Code closes, because `shutdownAction` is `none`. Ask whether the user wants them stopped, and give the WSL command:

```shell
docker compose --profile ase stop      # stops every database and keeps its data
```

- Never suggest `docker compose down -v` unless the user asks to reset data. It deletes every volume, including ASE's `master` device and the perf tables, which take minutes to reload.
- `docker compose down` without `-v` keeps the data too, but removes the containers, so ASE then needs `dev/ase/init.sql` again. `tests/common.py` applies it on the next test run.
- If the SySAM grace period in CLAUDE.md is less than 30 days away, say so.
