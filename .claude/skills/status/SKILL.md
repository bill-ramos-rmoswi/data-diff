---
name: status
description: Say what is running now in this repo (orchestrated runs, perf jobs, probes, test suites) and whether it's still making progress. Use when the user asks "what's happening?", "is it stuck?", "status on tests/perf?", or runs /status.
---

# What's running

Run `bash dev/status.sh` and summarize it in at most five lines:

- each orchestrated run in `.runs/`: its phase, its model, and what it's waiting on
- each long job: what it is, how long it has run, and its last progress line, as "n of m" with the time left when the line gives it
- anything flagged "no progress for N min". Say what that step normally takes (see "Performance tests" in CLAUDE.md). If it's longer than that, say it may be stuck, and offer to look. Don't kill anything without asking.

If you started a background task in this session, also give its state from the task list. A task whose output file says it finished at once, while `dev/status.sh` still shows the job running, was launched with `nohup` or `&`. Say so: its completion notice was false, and nothing will report when it really ends.

The user can run `bash dev/status.sh` in any terminal themselves, or `watch -n 30 bash dev/status.sh`. Mention that once, if they asked because they couldn't tell.
