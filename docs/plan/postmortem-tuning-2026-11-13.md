# Post-mortem: the orchestrated tuning run, 13 November 2026

This covers run `20261113-tuning`, the first task run with `/orchestrate`, from [Tuning-Orchestrated-Prompt.md](Tuning-Orchestrated-Prompt.md), and the v0.15.0 release that followed it.

It shipped PR #12. Db2, SQL Server and ASE to PostgreSQL diffs got 31–76% faster on `fact_2x`, `fact_13x` and `oltp_order_line` on the matched rig, and no checksum value changed. This document is about what the process taught us.

The figures come from `python dev/postmortem.py 20261113-tuning`, which reads the session transcripts and `.runs/20261113-tuning/ledger.jsonl`. They also come from the phase reports in that folder, and from `gh run list`. Times are UTC.

## What happened

| Phase | Agent | Model | Turns | Cache-read tokens | Wall | Result |
|---|---|---|---:|---:|---|---|
| Coordinator | `/orchestrate` | Sonnet | 109 | 10.6 M | 01:41–16:55 | Routed 8 phases and 7 jobs, 3 STOPs |
| 0 Rig and baseline | bench-runner | Sonnet | 9 | 0.2 M | 3 min + 87 min job | Rig bench. A/A median noise 5%, one row 20% (STOP 2) |
| 1 Where the time goes | prober | Sonnet | 75 | 3.6 M | 51 min | Seeded `oltp_order_line`, 39 profiles. Best: threshold 262144 for SQL Server |
| 2 Design | designer | Opus | 97 | 7.9 M | 17 min | Found the serial API default. 4 candidates (C, T1, D, B), no checksum change |
| 3 Implement, then sweep | implementer (one agent, resumed) | Opus | **254** | **46.2 M** | 1 h 38 min | 5 commits. A 144-run sweep kept the threshold at 16384 |
| 4 Tests | coordinator jobs | — | — | — | 4 min | 1,583 + 980 passed |
| 5 Measure | coordinator job | — | — | — | 1 h 43 min | 26 of 27 rows faster, 31–76% |
| 6 Review | general-purpose, plus a nested `/code-review high` | Opus | 131 + 65 | 14.3 + 6.1 M | 18 min | 3 fixed, 9 disproved, 8 accepted |
| Re-verify | coordinator jobs | — | — | — | 29 min | Suites and an oltp A/B again, after the review's fixes |
| 7 Docs and PR | docs-writer | Sonnet | 34 | 1.5 M | 2 min | PR #12, CI green |
| Release | a separate session, `/ship` | Sonnet | 36 | 2.1 M | 17:03–19:59 | PR #12 merged, version-bump PR #13, v0.15.0 tagged |
| **Total** | | | **~810** | **~93 M** | | |

For comparison, the fewer-casts phases took about 36 M cache-read tokens and found no gain ([postmortem-fewer-casts-2026-11-12.md](postmortem-fewer-casts-2026-11-12.md)). The ASE one-shot took 188 M ([retro-2026-11-11.md](retro-2026-11-11.md)).

The run took 15 hours of wall time, from 01:41 to 16:55. Of that, 8 h 13 min was STOPs waiting for an answer. The release added 3 hours.

## What went well

- **The user wasn't the scheduler.** One `/orchestrate` command ran every phase on the model the plan named. The only inputs were three STOP answers, and each was the recommended option. A Sonnet coordinator cost 10.6 M tokens for the whole run.
- **The run was visible.** The user checked `/status` four times from a second session while jobs ran, and got a correct answer each time. No job looked stuck. Every long job wrote `[i/N]` lines, and every completion notice was real.
- **The gates stopped code that wouldn't pay.** Candidate B (a threshold hint per engine) looked like −33% to −47% for SQL Server in phase 1. The 144-run sweep after C, T1 and D showed no threshold beyond 2× the noise, so no code was written. A faster hash than MD5, a dynamic factor and per-connection engine settings were dropped at design, each with its arithmetic.
- **The designer questioned phase 1's numbers.** It found that `diff_tables()` defaulted to a pool of 1, so segments ran one at a time, and that SQL Server's cost was the `MAX` types, not MD5. Those two findings were most of the gain.
- **The review reproduced before it fixed.** Each of 20 findings was fixed, disproved or accepted with a probe or a failing test. One was a silent wrong result (F1) that no test had caught.
- **Correctness held.** No checksum value changed (`fact_13x` 613085868081631030 and `oltp_order_line` -52598743289355856 on all four databases). Every `run_perf.py` check passed.
- **`/ship` caught a version clash.** At release, it noticed that `v0.14.0` already existed and that the package still said 0.14.0, and asked before bumping.

## What could have gone better

1. **STOPs waited 8 hours, and no push reached the user.** All three `PushNotification` calls returned "Mobile push not sent (Remote Control inactive)", and the coordinator carried on as if they had been sent.
   - **STOP 2 waited 6 h 18 min** (03:11–09:30). It was one A/A row at 20%, `Db2:PostgreSQL fact_2x identical`, a 1.6-second diff. The other 17 rows were 3–7%. A rule that gates each row on its own noise, set in advance, wouldn't have stopped.
   - **The design question waited 1 h 54 min.** Should the API thread-pool default and the leaf row hash be on by default? MY ANSWERS could have decided that before the run.
2. **One Opus implementer took half the run's tokens.** It ran 254 turns and 46.2 M tokens over four changes: C (hash input, which needed Opus), T1 (a default), D (leaf comparison) and a test fix. The coordinator then resumed it to read the threshold sweep. Each resumption re-read a context that kept growing.
3. **The review came after the measurement.** Phase 6 found three bugs in D:
   - mixed-case text UUID keys lost differences silently
   - a key that wasn't a UUID aborted the diff
   - a leaf where most rows differed was 2–4 times slower

   Their fixes meant running both suites and an oltp A/B again (29 minutes). The implementer's tests used integer keys and sparse changes only. Nothing in the agents or skills asked for other key shapes or densities.
4. **Phase 1 profiled a setting users don't get.** `profile_diff.py` ran with the API default of 1 thread, where the CLI uses `2 × threads`. Its best threshold (262144) was an artifact of serial queries, and was gone once T1 was in.
5. **The design estimated oltp from hand-written SQL.** Its probe modelled the SQL Server `numeric` columns as `DECIMAL`. The seeded table has `numeric`, which `MsSQL` doesn't map (`UnknownColType`, hashed as `NVARCHAR(MAX)` text), and three `varchar(8000)` columns. So C's predicted 57% on oltp didn't apply. The implementer found this, and it's now a follow-up.
6. **The oltp table's noise was assumed.** Phase 0 ran A/A before phase 1 seeded `oltp_order_line`, so the design gate used an assumed 7%. In the end, `ab.py` measured 1–14% in the same runs.
7. **A long check ran in the foreground.** `rig.py check --noise 5` was killed at 115 s. `bench-runner` describes the check as "about 10 seconds", which is true only without `--noise`.
8. **The release was an afterthought.** The plan stopped at the PR, so the release took a second session and 3 hours:
   - The version bump needed its own PR (#13), with CI on the PR, on master and in `release.yml`.
   - The remote branches were left behind.
   - The notes were GitHub's auto-generated list, with no upgrade note for the changed API default.
   - The wheel was never installed before it was published.
   - There were no checksums, provenance or SBOM.

## What changes

| Problem | Change |
|---|---|
| 1. STOPs that wait, and unsent pushes | `/orchestrate` sends a test push at setup, and says when STOPs won't reach a phone. Plans have a **PRE-DECIDED** block: the noise rule, which defaults may change, whether checksums may change, the version rule, and how far the run may go. The noise gate is per row by default. STOP 2 fires only when the rig isn't bench or the median noise is over 15%. |
| 2. One expensive implementer | `/orchestrate` spawns one fresh `implementer` per plan step, on the model the design gives that step. The `designer` labels each step `sonnet` or `opus`, with a reason. Benchmark output goes to `bench-runner` or `prober`, never back to an implementer. |
| 3. Review after measure, and tests on one key shape | The template's order is implement → review → tests → measure. `cross-db-design`, `implementer` and `/test` add a key-shape and density matrix:<br>- key shapes: integer, composite, native UUID, mixed-case text UUID, non-UUID text among UUIDs, NULL-heavy rows<br>- densities: none, sparse, more than half, all |
| 4. Profiling a setting users don't get | `prober` prints the differ settings in each report's header, and checks them against the CLI's and API's defaults. |
| 5. Estimates from hand-written SQL | `designer` estimates from the SQL data-diff generates on the seeded tables (`LOG_LEVEL=DEBUG`), and records the column type detected per dialect. |
| 6. Assumed noise | `bench-runner` and `/orchestrate`: A/A runs on every table shape in scope, including one seeded during the run, before the design gate uses its noise. |
| 7. A foreground `--noise` | `bench-runner` and `/perf`: `rig.py check --noise` is always a background job. |
| 8. Release as an afterthought | The release now has a CHANGELOG, a wheel smoke test, checksums, an SBOM and provenance:<br>- **CHANGELOG:** `CHANGELOG.md`. `dev/release/notes.py` turns its section into the release body, and fails when the section is missing.<br>- **Wheel smoke test:** `dev/release/smoke_wheel.py` installs the wheel with its extras in a clean venv and runs a diff. It runs on every PR (the `wheel` job) and in `release.yml` before publishing.<br>- **Assets:** `release.yml` attaches `SHA256SUMS` and an SPDX SBOM, and signs build provenance.<br>- **`/ship`:** the version bump and CHANGELOG go in the feature PR, and merging deletes the branch.<br>- **Plans:** a run may go through release, with one STOP before the merge. |
| Writing this document | `/post-mortem` and `dev/postmortem.py` collect these numbers for any run. |

The next run starts from [Followups-Orchestrated-Prompt.md](Followups-Orchestrated-Prompt.md), built from [Orchestrated-Plan-Template.md](Orchestrated-Plan-Template.md).

## Open findings, carried forward

- **SQL Server `numeric` isn't in `TYPE_CLASSES`.** It's hashed as `NVARCHAR(MAX)` text, so it never takes the bounded hash input. That's in the follow-up plan.
- **The `|` blind spot** (`'a|b','c'` against `'a','b|c'`) is in checksums, `RowHashDiffer` and leaf row hashes. The follow-up closes it in the leaf only, without changing checksums.
- **Mixed-case text UUID keys give false positives between Db2 and PostgreSQL** (18 diffs reported where 4 exist). It happens with or without the leaf, and is already on master. Key ranges order mixed case differently on Db2 (binary) and PostgreSQL (locale). No rows are lost.
- **`self.stats[...]` updates from worker threads can lose a count** (review R6). Not reproduced, and only informational counters depend on it.
- **Db2 `DATE` against PostgreSQL `TIMESTAMP(0)` at midnight** (from the fewer-casts run) still hasn't been checked on master.
- **`TestSybaseConcurrency` closed a shared connection under another thread.** The test is fixed, and CLAUDE.md documents the rule. A reference-counted `close()` would change the API, so it wasn't done.
