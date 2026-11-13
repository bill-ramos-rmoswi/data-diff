---
name: hello
description: Start-of-session checklist for this repo. Use when the user says hello, is starting or resuming work, has just opened or rebuilt the devcontainer, or runs /hello. Checks git identity, gh login, databases and where work was left, and fixes what it can for the session.
---

# Starting a session

Check everything below, then give one short report: what's ready, what you fixed, and what the user must do. Read-only checks need no permission. Ask before anything that changes the repo, GitHub or a database. This is the counterpart of `/shutting-down`.

## 1. Where this session runs

`/.dockerenv` exists in the devcontainer, not in WSL. The devcontainer has no Docker socket, so anything that starts a database is a command for the user to run in a WSL terminal.

## 2. Git identity

```shell
git config user.name
git config user.email
```

- No name: ask for it.
- An email that doesn't end in `@users.noreply.github.com` puts a personal address in public commits. Get the noreply address from `gh api user -q '"\(.id)+\(.login)@users.noreply.github.com"'`. Set it with `git config --global user.email ...`, which lasts until the container is rebuilt, and say you did. Tell the user to set the same in WSL, because VS Code copies the WSL `~/.gitconfig` into the container at each rebuild.
- Commits on the current branch that aren't on `origin` and carry another email: list them. Fixing them means `git commit --amend --reset-author` or a rebase, so ask first.

## 3. gh

Run `gh auth status`. If it isn't logged in, the user runs `gh auth login` in WSL or here. Both use the same `~/.config/gh`, so once is enough. Don't run it yourself: it's interactive.

## 4. Databases

For each `DATADIFF_*_URI` that's set, check that its host and port accept a connection, with a one-second timeout, for example `python3 -c "import socket; socket.create_connection(('db2', 50000), 1)"`. In the devcontainer the hosts are compose service names. Report each as up or down.

- PostgreSQL, MySQL, SQL Server or Db2 down: they start with the devcontainer, so the user runs `docker compose up -d --wait postgres mysql mssql db2` in WSL. Db2 takes a minute or more when its volume is new.
- SAP ASE down with `DATADIFF_SYBASE_URI` set: `tests/common.py` stops at import. The user either runs `docker compose --profile ase up -d --wait ase` in WSL, or skips ASE for this session with `unset DATADIFF_SYBASE_URI` in their terminal. That `unset` doesn't reach your own shell, so pass `env -u DATADIFF_SYBASE_URI` to test commands instead.
- If the SySAM grace period in CLAUDE.md is less than 30 days away, say so.

## 5. Where work was left

```shell
git fetch --quiet origin
git status --short --branch
git stash list
git log --oneline HEAD..origin/master | head
```

- Uncommitted changes, stashes, or commits that aren't pushed: list them. They're probably from the last session.
- If master has moved on and the current branch isn't master, say how many commits behind it is.
- Open PRs and the latest CI runs: `gh pr list --repo bill-ramos-rmoswi/data-diff --author @me` and `gh run list --repo bill-ramos-rmoswi/data-diff --limit 5`. Report failed or running checks.
- The `handoff-*` project memories written by `/shutting-down` (and an old `handoff`, if any): give each one's next step and the model it names. Also mention any other memories that describe work in progress.
- Run `bash dev/status.sh`. Report orchestrated runs in `.runs/` that haven't finished, and jobs still running.
- If `.runs/rig.json` exists and doesn't say `"profile": "bench"`, note that the databases aren't on the matched benchmark rig. That only matters for benchmarks.

## 6. Report

At most ten lines. Lead with anything the user must do in WSL, as a command they can paste. End with the next step from the last handoff, or a question about what to work on.
