Written for: a fresh Claude Code session in the devcontainer, opened from `~/repos/data-diff`, and run one phase per session.

This prompt takes on one of the speed-ups that [technical-explanation.md](../technical-explanation.md) lists as not yet implemented: "faster checksums by reducing type-casts". A faster hash than MD5 and dynamic bisection tuning are separate tasks.

Unlike [SAP-ASE-One-Shot-Prompt.md](SAP-ASE-One-Shot-Prompt.md), this runs in phases, each in its own session, so no context grows to the 600 turns the ASE work took (see [retro-2026-11-11.md](retro-2026-11-11.md)). Each phase names the skill or agent to use and the model to switch to with `/model`. Each phase ends with `/shutting-down`, which saves a `handoff` memory, and the next starts with `/hello`, which reads it. Paste the whole prompt into each session, and add "Do phase N".

Before the first session:

1. In WSL, start all the databases: `docker compose up -d --wait postgres mysql mssql db2` and `docker compose --profile ase up -d --wait ase`.
2. Seed the perf tables in the devcontainer if they aren't there yet: `poetry run python dev/perf/seed.py`. `/perf` checks the counts.
3. Make sure master CI is green and you're on the latest master.

```text
Make data-diff's hashdiff checksums faster by removing type-casts that aren't
needed, in bill-ramos-rmoswi/data-diff, without changing any checksum value
that a supported pair relies on. Work one phase per session. Stop only for a
STOP CONDITION, or at the end of the phase.

MY ANSWERS
- Pairs that matter: Db2 → PostgreSQL, SQL Server → PostgreSQL, ASE →
  PostgreSQL (on-prem to Aurora). All other pairs must still pass, but
  needn't get faster.
- Tables: fact_2x (1.55M rows) and fact_13x (10.09M), with INT keys, DATE,
  NUMERIC(19,4) and INT columns. Then the standard type suites.
- Settings: ASE at its 2 GB defaults, N_THREADS=4, hashdiff (rowhash only if
  a change also affects it).
- Compatibility: a checksum may change only if it changes identically on
  every dialect in the same release, and the README and release notes say
  so. Prefer changes that keep every value the same.
- Out of scope: replacing MD5, bisection tuning, client-side comparison.
- Done means a measured gain over the noise on at least one target pair, or
  evidence that the casts aren't where the time goes.

GROUND RULES
- Read CLAUDE.md first, especially "Cross-database matching", "Database
  behavior that's easy to get wrong" and "Performance tests".
- Branch perf/fewer-casts from the latest master.
- Probe with /db-probe (dev/probe.py), not with hand-written driver scripts.
  Put SQL files in the scratchpad.
- Run long jobs (tests, run_perf.py, CI watches) in the background, and wait
  for their notification. Don't poll with sleep.
- Keep large output out of the conversation: use /test, the test-runner
  agent and /ci-triage.
- You may commit and push the branch, and open one PR on the fork with
  --repo bill-ramos-rmoswi/data-diff. Don't merge, tag or stop services.
- Prefer dialect overrides to changes in base.py. A shared change must keep
  the SQL every other dialect produces identical, unless a test shows a bug.
- Keep a results log in the scratchpad and, from phase 7, in the PR body:
  each command, and what it printed.
- End every phase with /shutting-down. The handoff memory names the next
  phase and its model.

STOP CONDITIONS: stop and report, with evidence and options
1. A checksum for any supported pair stops matching, and the only fix
   changes values on a dialect that isn't part of the change.
2. Phase 1 shows the casts are less than 10% of checksum time on every
   target pair. Report where the time goes instead.
3. A cast removal is only safe on some values: Db2 truncation, ASE's 16 KB
   expression limit or cp850, SQL Server NVARCHAR, or the Db2 VARCHAR_FORMAT
   "####" overflow.
4. After 3 alternating runs each, the gain on every target pair is inside
   the ~20% run-to-run noise.
5. A change needs a database setting, UDF or image change on the target.

PHASE 0: BASELINE                         model: Sonnet   skill: /perf
- /hello. Confirm master CI is green, and that the perf tables are seeded on
  all four databases.
- /perf baseline on master: Db2:PostgreSQL, MsSQL:PostgreSQL and
  Sybase:PostgreSQL, fact_2x and fact_13x, run twice. Save the jsonl files
  under the scratchpad, outside the repo.
- Handoff: the baseline table (pair, table, scenario, median seconds).

PHASE 1: WHERE THE TIME GOES        model: Sonnet   skill: /db-probe   agent: explorer
- With the explorer agent, map the cast chain for each column type on each
  dialect: render_checksum → render_rowhash → normalize_value_by_type →
  normalize_number / normalize_timestamp / to_string → concat → md5_as_int,
  in data_diff/databases/base.py and the overrides in postgresql.py,
  mssql.py, db2.py and sybase.py. Give a table of type, dialect and the SQL
  emitted.
- Capture the real checksum SQL for fact_13x on each database with
  LOG_LEVEL=DEBUG on one small diff.
- With /db-probe --repeat 3 on fact_13x, time each database against its own
  copy (--perf on ASE). Time these, one column at a time and then all:
  count(*); sum(md5_as_int(<concat of raw keys>)); the full checksum; and
  the full checksum with each normalize or cast swapped for the raw column.
  This gives the share of time spent on casts, per type and per dialect.
- Decide STOP CONDITION 2 here.
- Handoff: the cost table, ranked, with the three candidates worth the most.

PHASE 2: DESIGN        model: Opus or Fable   plan mode   skill: /cross-db-design
- For each candidate, fill the /cross-db-design behavior table, with probed
  values, on all five dialects, MySQL included. Ideas to test, not to
  assume:
  - integers: skip to_string where every dialect renders an integer the
    same, or cast once per row instead of once per column
  - NUMERIC(p,s) with no rounding: a cheaper rendering that gives the same
    text
  - DATE columns: whether the timestamp normalization is needed, or a
    date-only form gives the same text
  - merging nested casts (for example a cast to varchar that wraps a format
    that already returns text)
  - SQL Server: NVARCHAR(MAX) only where text needs it, and no FORMAT()
- Each design states whether checksum values stay the same. If they don't,
  that's STOP CONDITION 1 unless every dialect changes together.
- Pair every change with a test that fails on a real difference.
- Handoff: the approved plan, its file list, and the expected gain per pair.

PHASE 3: IMPLEMENT                          model: Opus (Sonnet for the rest)
- Implement the plan, dialect overrides first. Run poetry run ruff format .
- After each dialect, quick-check with /test -k <Dialect> and /db-probe that
  the checksum of fact_2x still equals PostgreSQL's.
- Commit per logical change.

PHASE 4: TESTS                      model: Sonnet   skill: /test   agent: test-runner
- Add tests: for every type touched, a "should match" test and a
  "difference is reported" test, at each side's lowest precision, with
  NULL, the empty string, negative under 1, the maximum precision and the
  largest dates.
- Hand the full suite to the test-runner agent with TEST_ACROSS_ALL_DBS=full,
  then the whole suite with unittest-parallel -j 8. Check that "Testing
  databases:" lists all five.

PHASE 5: MEASURE                                   model: Sonnet   skill: /perf
- /perf on the branch with the same pairs and tables as phase 0. Alternate
  master and branch, 3 runs each.
- Report the before and after table with % change and the median of 3.
  Decide STOP CONDITION 4.

PHASE 6: REVIEW                       model: Opus   skill: /code-review high
- Run /code-review high on master..HEAD. Also do your own pass, aimed at
  values that would now hash the same, and silent truncation.
- Reproduce each finding with /db-probe or a failing test before fixing it.
  Report each as fixed, disproved, or accepted with a reason.
- Rerun /test for what the fixes touched, and /perf once on the final
  commit.

PHASE 7: SHIP                        model: Sonnet (Haiku for triage)
                                     skills: /ship, /ci-triage   agent: docs-writer
- With docs-writer, update:
  - CLAUDE.md: any new behavior that's easy to get wrong
  - README.md: the benchmark table for the three target pairs, with the
    revision
  - docs/technical-explanation.md: remove "reducing type-casts" from the
    list of ideas not yet implemented, if it's done
- /ship: push, open the PR with the results log, and watch CI in the
  background. On a failure, use /ci-triage. Don't merge.

DONE MEANS
- The full suite and CI pass on the same final commit, with all five
  databases listed as tested.
- Every target pair reports exactly the injected differences at 10M rows,
  and zero on identical tables.
- The gain is shown beyond the noise, or STOP CONDITION 2 or 4 is reported
  with evidence.

FINAL REPORT (end of phase 7)
Give the PR link, then:
- the before and after table, with the ASE configuration
- the cast cost table from phase 1
- which checksums changed (ideally none)
- review findings by outcome
- what was not tested
Keep what you observed separate from what you assume.
```
