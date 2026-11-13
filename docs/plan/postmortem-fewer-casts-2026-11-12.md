# Post-mortem: faster checksums by reducing type-casts, 11–12 November 2026

This covers the first task run in phases, from [Faster-Checksums-One-Shot-Prompt.md](Faster-Checksums-One-Shot-Prompt.md). Its goal was faster hashdiff checksums, with no checksum value changing.

The task stopped at STOP CONDITION 4. Full diffs were 0–5% faster, inside the noise. The work is kept at the tag `archive/perf-fewer-casts`, and [technical-explanation.md](../technical-explanation.md) records what was tried (PR #9).

Not finding a gain is an acceptable result. This document is about what the process taught us.

The figures come from the devcontainer transcripts in WSL, `~/.claude-devcontainer/projects/-workspaces-data-diff/`, and from `git log` and `gh run list`. Times are UTC.

## What happened

| Phase | Session | Model | Turns | Cache-read tokens | Result |
|---|---|---|---:|---:|---|
| 0 Baseline | 83f58333 | Opus (the plan said Sonnet) | 38 | 2.3 M | Baseline on master for the three target pairs |
| 1 Where the time goes | 7376ea44 | Sonnet | 72 | 5.7 M | Cast cost per type and dialect. Then, at the user's prompt, the MAXDOP finding |
| 2 Design | c1c7a64b | Opus | 125 | 12.5 M | Four Db2 changes and one ASE change, no checksum changes. Predicted −16% (Db2) and −8% (ASE) **on the checksum alone** |
| 3 Implement | ae686d55 | Opus | 105 | 10.1 M | 6 commits, plus `dev/perf/checksum.py` |
| 4 Tests | e7774daa | Sonnet | 33 | 2.3 M | 1,592 tests passed. Checksums identical to master |
| 5 Measure | 68a680a8 | Sonnet | 36 | 2.2 M | 3 alternating runs each. Full diffs 0–5% faster: STOP 4 |
| Archive and docs | c7163c83 | Sonnet | 19 | 1.0 M | Tag, note in technical-explanation.md, PR #9 merged |
| **Total** | | | **~430** | **~36 M** | |

For comparison, the ASE one-shot took 600 turns and 188 M cache-read tokens in a single session ([retro-2026-11-11.md](retro-2026-11-11.md)).

Wall time was about 5 hours, 21:33 to 02:32, including 2 hours 13 minutes the user was away.

## What went well

- **Phases kept the cost down.** About 36 M cache-read tokens against 188 M for the one-shot. Each session started small, and the biggest (design, 125 turns) was a fifth of the one-shot.
- **The handoff worked between sequential sessions.** `/shutting-down` wrote the next step and its model, and `/hello` read it, so no session started from pasted output.
- **Correctness held.** All 1,592 tests passed. The full `fact_13x` checksum stayed `613085868081631030` on PostgreSQL, Db2 and ASE, and every `run_perf.py` correctness check passed.
- **The stop conditions did their job.** STOP 4 ended the work with evidence. Archiving to a tag and writing up the result took one short session, with nothing half-merged.
- **The user's skepticism found the real issue.** "SQL Server time is mostly in HASHBYTES... this seems odd" led to the MAXDOP probe. It showed that the rig, not the SQL, made SQL Server slow.
- **The new tools paid off.** `/db-probe` (`dev/probe.py`) replaced hand-written driver scripts, and `test-runner` kept 1,592 test results out of the conversation.

## What could have gone better

1. **The phases needed the user as the scheduler.** Each of the 7 sessions needed `/model`, a paste of the prompt, "Do phase N", `/hello` and `/shutting-down`. Phase 0 ran on Opus where the plan said Sonnet, because the switch was forgotten. Nothing in the plan needed the user between phases, except at a STOP.
2. **Jobs looked stuck, and the user couldn't tell.** There were three cases:
   - **Phase 1, 22:04.** A timing script ran as `nohup bash runall.sh` inside a background command. The launcher exited at once, so the "completed" notice came before any work. The session said it would report when the job finished, and ended its turn, and nothing was left to wake it. The user asked "What's happening now?" 16 minutes later, then "ASE should not be taking this long".
   - **Phase 1, 22:41 and 01:05.** Two foreground commands ran for about 10 minutes each (590 s and 588 s), with no output until the end.
   - **Phase 5.** A 65-minute A/B job printed no progress. The user asked "status on tests?" at 00:50 and "status on perf job" at 01:08.
3. **The rig wasn't matched, and we measured it anyway.** `cpus: 4` is a CFS quota, not a pin, so each engine saw all 16 CPUs.
   - **SQL Server thrashed.** It started 16 workers on a 4-CPU quota. The same MD5 query on `fact_13x` took:

     | MAXDOP | Seconds |
     |---|---:|
     | 0 (default) | 47 |
     | 1 | 56.8 |
     | 2 | 25.3 |
     | **4** | **14.6** |
     | 8 | 22.1 |
     | 16 | 35.5 |

   - **The finding was left out of the benchmark.** The session that found it advised fixing the rig before phase 5, but phase 5 ran without the fix. The SQL Server baselines, and probably the README's MsSQL rows, measure the thrash.
   - **Memory differed too:** PostgreSQL 4 GB, SQL Server 6, Db2 10, ASE 2.
   - **Unchecked:** whether Db2 (`INTRA_PARALLEL` off by default) and ASE (engine count) used their 4 CPUs.
4. **The outcome was predictable before implementation.**
   - Phase 2's own numbers predicted 16% and 8% on the checksum query. A full diff also finds key ranges, bisects and downloads rows. So the end-to-end gain had to be smaller, and it was under the 20% noise even before that.
   - Phase 1 measured each cast's share of the checksum, not the checksum's share of the diff. STOP 2 asked the wrong question.
   - A gate that multiplied the two shares (Amdahl's law) would have stopped the work after phase 2, and saved phases 3–5: about 175 turns and 15 M tokens.
5. **The noise threshold was a guess.** "About 20%" came from earlier runs, not from a measurement in the same comparison. Phase 5's own spreads were mostly smaller (for Db2 `fact_13x` deleted: 61.3–68.5 s, 11%). A rule based on noise measured in the same runs, with 5 runs instead of 3, would have given a firmer verdict either way.
6. **One handoff memory for every session.** Phase 1's session was resumed after phases 2–4 had run in other sessions. It read a `handoff` they had overwritten, and reported the wrong state.
7. **Smaller friction.** Each cost a few turns:
   - Grep wasn't available in one session.
   - SQL Server's 254-argument limit on `concat`.
   - pyodbc can't fetch `sql_variant` (ODBC type -150).
   - An ASE arithmetic overflow while converting a hash.
   - Phase 3 needed the user to name the plan file's path.

## What changes

| Problem | Change |
|---|---|
| 1. The user as the scheduler | **`/orchestrate`** (`.claude/skills/orchestrate/SKILL.md`). A Sonnet coordinator spawns each phase as a background agent on the model the phase names, and checks the stop conditions itself from the numbers the agent returns. It interrupts only at a STOP or when done, with `PushNotification` and a question. New phase agents: `prober` (Sonnet), `designer` (Opus), `implementer` (Sonnet, Opus when told), `bench-runner` (Sonnet). |
| 2. Jobs that look stuck | **`dev/status.sh`** and **`/status`**. Any terminal can see each run's `.runs/<run-id>/status.md`, the jobs running, and their last progress line, with a flag after 5 minutes without progress. The new **"Long jobs" rules in CLAUDE.md**: foreground under 2 minutes, never `nohup` or `&` in a background command, progress lines for long jobs, and a one-line note of what's running. `run_perf.py` now prints timestamped `[i/N]` lines with the time left. |
| 3. Unmatched rig | **`docker-compose.bench.yml`**: 4 pinned vCPUs (`cpuset`) and 4 GB for every database. **`dev/bench/rig.py apply`** gives each engine a cache of about 2 GB and 4-way parallelism (SQL Server MAXDOP 4, Db2 `INTRA_PARALLEL`, ASE 4 engines, PostgreSQL 4 workers). **`rig.py check`** reads it all back, with the CPUs each engine sees, and writes `.runs/rig.json`, which every benchmark row now records. |
| 4. Predictable outcome | **`dev/perf/profile_diff.py`** splits a diff's wall time into checksum, key range, download and client-only time. `/orchestrate` has an **Amdahl gate** after design: step share × expected speed-up must exceed twice the noise, or it's a STOP before any code. `dev/perf/checksum.py` is restored from the archive, for timing the checksum alone. |
| 5. Guessed noise | **`dev/perf/ab.py`** alternates base and head 5 times each, from a worktree, and its verdict counts a change only beyond twice the noise measured in those runs. `--base HEAD` is an A/A run, which measures the noise alone. |
| 6. One handoff | Handoffs are now `handoff-<topic>` (the run id for an orchestrated run), and `/hello` reads them all. |

The next tuning effort starts from [Tuning-Orchestrated-Prompt.md](Tuning-Orchestrated-Prompt.md), which uses all of the above.

## Open findings, carried forward

- **Re-measure SQL Server on the matched rig.** That covers the MsSQL:PostgreSQL baseline and the README's MsSQL rows. Expect them to be much faster.
- **Db2 `DATE` against PostgreSQL `TIMESTAMP(0)` at midnight gave different checksums on the branch.** Check it on master. It may be a correctness bug, not a speed issue.
- **MySQL renders `DATE` with a time part** (`00:00:00.000000`), unlike the other four databases.
- **Db2 renders integers as `VARCHAR(VARCHAR(x))`,** a double cast. This wasn't tried.
- **Things the fewer-casts work didn't measure:** whether Db2 and ASE use their 4 CPUs on the old rig. `rig.py check` now shows it.
- **The SQL Server alternatives the user asked about.** `CHECKSUM` and `BINARY_CHECKSUM` take 0.88 s, but they're 32-bit and exist only on SQL Server, so other databases can't match them. `HASHBYTES` with SHA1 or SHA2_256 costs the same as MD5. Hashing in Python takes about 0.38 s per million rows for MD5, but it's a different algorithm (`RowHashDiffer` already exists for that). None of these was a cast change, but the next effort may want them.
