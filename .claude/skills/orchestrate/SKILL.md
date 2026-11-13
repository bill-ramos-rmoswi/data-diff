---
name: orchestrate
description: Run a phased plan (such as docs/plan/Tuning-Orchestrated-Prompt.md) as a coordinator that hands each phase to a background agent on the model the phase names, checks the stop conditions itself, and interrupts the user only at a STOP or when done. Use when the user runs /orchestrate, or asks to run a phased plan without a session per phase.
---

# Running a phased plan

You are the coordinator. You don't do the phases' work yourself: each phase runs in its own agent, so your context only holds short reports, and each phase gets the model it needs. Read "Long jobs" in CLAUDE.md first.

## 1. Set up the run

- Read the plan the user names. It must have, for each phase, a header with `model`, `agent`, `goal`, `outputs`, `returns` (the `KEY=value` lines the stop checks need) and `stop` (which stop conditions to check after it). If a phase lacks one, ask once, before starting, and not later.
- Pick a run id, `<yyyymmdd>-<slug>`, and create `.runs/<run-id>/`. It's git-ignored. In it:
  - `status.md`: the run, the plan, the phase and step running now, its agent and model, when it started, when it should finish, and the next thing you'll do. Rewrite it at every event. `dev/status.sh` prints it, so the user can see the run from any terminal.
  - `ledger.jsonl`: one line per event (`phase_start`, `phase_done`, `job_start`, `job_done`, `stop`, `resume`), with the time and the returned values.
  - `phase-<n>.md`: each phase agent's full report, written by the agent.
- Load `PushNotification` with ToolSearch now, so a STOP doesn't wait on it.
- Tell the user in at most three lines: the run id, the phases with their models, and `bash dev/status.sh` to watch. That is the last thing they need to read until a STOP or the end.

## 2. Run each phase

1. Update `status.md` and the ledger.
2. Spawn the phase with the Agent tool: `subagent_type` from the plan (one of `.claude/agents/`, or `general-purpose`), `model` from the plan, and a prompt that has:
   - the plan's MY ANSWERS, GROUND RULES and STOP CONDITIONS, verbatim
   - this phase's section, verbatim
   - the paths of the earlier `phase-<n>.md` reports it needs. Point to them, and don't paste them
   - "Write your full report to `.runs/<run-id>/phase-<n>.md`. Write progress lines with a timestamp to `.runs/<run-id>/progress-phase-<n>.log` at each step. Reply with at most 15 lines: a one-line summary, then the `KEY=value` lines from `returns`, then `STOP_CANDIDATE=<number and reason>` if you hit a stop condition. Don't ask the user anything: you can't, so report a decision you need as a STOP_CANDIDATE."
3. It runs in the background. End your turn with one line saying which phase is running on which model, and when you expect it back. You'll be woken by its notification, which comes from the agent's real work, unlike a launcher that exits at once.
4. When it returns, record the values in the ledger, and check the stop conditions the phase names, yourself, from the returned numbers. Don't take an agent's word that a gain is real: apply the noise rule below.
5. No STOP: start the next phase in the same turn.

## 3. Long jobs

A job that takes more than 10 minutes, such as `ab.py`, a full test suite or a seed, is yours, not the agent's: an agent's own background jobs may not wake it.

- The phase agent prepares the job and returns its command as `JOB=<command>`, and stops.
- You run it with `run_in_background`, with its progress going to a file under `.runs/<run-id>/`. Never wrap it in `nohup` or end it with `&`: the notification would come when the launcher exits, at once, which is how a run looked stuck on 11 November.
- Put the job, its progress file and its expected time in `status.md`. If `Monitor` is available, use it on the progress file, so new lines reach you. Either way, end your turn. The job's notification wakes you.
- When it's done, spawn the phase agent again, or the next phase, with the job's output file.

## 4. Gates you always apply

- **Noise:** a change is a result only if it's more than twice the noise measured on this rig. Take the noise from phase 0's A/A run, or from `ab.py`'s summary, not from a fixed 20%.
- **Amdahl:** after a design phase, take the share of wall time from `dev/perf/profile_diff.py` (phase 1) for the step the design speeds up. Multiply it by the design's expected speed-up of that step. If the product is under twice the noise on every target pair, it's a STOP before implementing, because it can't be measured.
- **Correctness:** a failing test, a checksum that changed when the plan says values stay the same, or a `run_perf.py` correctness failure is always a STOP.

## 5. At a STOP

1. Write the stop to the ledger and `status.md`.
2. Send `PushNotification` with one line: the run id, the phase and the stop condition.
3. Ask with AskUserQuestion. The question names the stop condition and gives the evidence in at most three lines, with the numbers. The options are the ways on that you see, with your recommendation first. "Stop and archive" is always one of them.
4. Carry on as the answer says, and write a `resume` line to the ledger.

## 6. At the end

- Send `PushNotification`: done, with the PR link if there is one.
- Give the plan's FINAL REPORT. Take it from the `phase-<n>.md` reports, read only for this.
- Write a `handoff-<run-id>` project memory, as `/shutting-down` describes. Name it after the run, so a session that runs in parallel doesn't replace it.

## Choosing models

When the plan doesn't say:

| Work | Model |
|---|---|
| Finding code, reading CI logs, the status of a run | haiku (`explorer`, `ci-triage`) |
| Probes, timing, tests, benchmarks, implementing a settled design, docs, shipping | sonnet (`prober`, `bench-runner`, `implementer`, `test-runner`, `docs-writer`) |
| Cross-database design, hash and encoding bugs, `/code-review high` | opus (`designer`), or fable for the hardest |
| Coordinating | sonnet: you only route, record and check numbers |

## Don't

- Don't merge, tag, stop services or change a database setting unless the plan allows it. A change on the target is a STOP.
- Don't paste a phase report into the next prompt. Point to the file.
- Don't run probes or tests yourself between phases. Spawn an agent, so your context stays small.
