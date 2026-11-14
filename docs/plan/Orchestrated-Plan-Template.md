Written for: you, to copy for a new phased task, fill in, and run with `/orchestrate <file>` in one session on Sonnet.

This template is [Tuning-Orchestrated-Prompt.md](Tuning-Orchestrated-Prompt.md) reshaped by [postmortem-tuning-2026-11-13.md](postmortem-tuning-2026-11-13.md). What changed and why:

- **PRE-DECIDED** settles in advance the questions that made the tuning run wait 8 hours at STOPs.
- **Each implementation step** names its model, so only hash and encoding work runs on Opus.
- **Review comes before tests and measurement**, so its fixes don't cost a second measurement.
- **The run can go through release**, with one STOP before the merge.

Before you start:

1. **Benchmarks:** if the task involves any, start the databases on the matched rig in WSL:
   ```shell
   docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --wait postgres mysql mssql db2
   docker compose -f docker-compose.yml -f docker-compose.bench.yml --profile ase up -d --wait ase
   ```
   Then run `poetry run python dev/bench/rig.py apply` in the devcontainer, and the Db2 restart it prints in WSL.
2. **Notifications:** turn on Remote Control in Claude Code, so that a STOP reaches your phone. Without it, the run waits until you look. `bash dev/status.sh` shows it in any terminal.
3. Fill in MY ANSWERS and PRE-DECIDED.
4. Start a session on Sonnet, and run `/orchestrate docs/plan/<this file>`.
5. When it's done, run `/post-mortem <run-id>`.

```text
<one paragraph: the goal, the repository, and the one constraint that matters most,
for example "without changing any checksum value">

MY ANSWERS
- Pairs that matter: <default: Db2, SQL Server, ASE -> PostgreSQL. All other pairs must still pass>
- Tables: <fact_2x, fact_13x (dev/perf/seed.py), oltp_order_line (dev/perf/seed_oltp.py)>
- Settings: matched rig (4 vCPU / 4 GB each), N_THREADS=4, the differ's default thread pool
- Scope, in order: <items>
- Out of scope: <items>
- Done means: <a gain beyond 2x each row's noise on one target pair, or a fix with tests that
  would fail without it, or evidence of why not>

PRE-DECIDED  (the coordinator applies these without asking)
- Noise: per row. A row's change counts only beyond 2x its own A/A noise. STOP 2 only when the
  rig isn't bench or the median noise of the target rows is over 15%.
- Public defaults that may change: <none | list, e.g. "API thread pool default">
- Checksum values: <may not change | may change on every dialect together, with an upgrade note>
- Version: SemVer from the change. A changed default or result order is minor, fixes are a patch.
  The bump and the CHANGELOG section go in the PR.
- How far: <PR only | PR, then merge on CI green | through release, one STOP before the merge>
- Test-only fixes for a test's own misuse (for example a shared connection): STOP, with the fix ready.

GROUND RULES
- Read CLAUDE.md first, especially "Long jobs", "Cross-database matching",
  "Database behavior that's easy to get wrong" and "Performance tests".
- Branch <prefix>/<name> from the latest master.
- Probe with dev/probe.py, dev/perf/checksum.py and dev/perf/profile_diff.py. Profile at the
  differ settings users get. Estimate from the SQL data-diff generates on the seeded tables.
- Keep files under .runs/<run-id>/, not in the repo.
- Benchmarks only on the matched rig: dev/bench/rig.py check must say "Rig: bench".
- Every table in scope has an A/A measurement before a gate uses its noise.
- Tests for a differ, key-range or leaf change cover the key shapes and densities in
  /cross-db-design.
- Prefer dialect overrides to changes in base.py.
- Don't stop services or run rig.py apply. Merge and tag only as "How far" allows.
- End commit messages with the attribution line from the session.

STOP CONDITIONS: stop and report, with evidence and options
1. A checksum for a supported pair stops matching, and PRE-DECIDED says values may not change.
2. The rig isn't "bench", or the median A/A noise of the target rows is over 15%.
3. Amdahl: for every candidate, the step's share of wall time times its expected speed-up is
   under 2x the measured noise, on every target pair.
4. After measuring, no target row shows a gain beyond 2x its noise (performance work only).
5. A change needs a server setting, UDF or image change on the target.
6. Any test fails, or run_perf.py reports wrong rows.
7. A decision PRE-DECIDED doesn't cover: a public default, result order, or API change.
8. Release: the PR's CI is green. Ask once to merge and release (only when "How far" says so).

PHASE 0: RIG, SEED AND NOISE
model: sonnet   agent: bench-runner
goal: Rig check, tables seeded, A/A on every table in scope (a coordinator JOB).
returns: RIG, NOISE_PCT, NOISY_ROWS
stop: 2

PHASE 1: WHERE THE TIME GOES / WHAT THE CODE DOES
model: sonnet   agent: prober
goal: profile_diff.py per target pair and table shape, at the defaults users get. Report the differ
  settings first. For a correctness task: reproduce the problem with a probe or failing test.
returns: SHARE_<step>_<pair>_<shape> | REPRODUCED
stop: none

PHASE 2: DESIGN
model: opus   agent: designer
goal: The /cross-db-design table, from generated SQL and detected types on the real tables.
  Expected end-to-end gain with its arithmetic. Numbered steps, each with model and tests.
  Changed defaults listed against PRE-DECIDED. The version and CHANGELOG lines.
returns: EXPECTED_GAIN_PCT_<pair>, CHECKSUMS_CHANGE, STEPS, VERSION, PLAN
stop: 1, 3, 5, 7

PHASE 3: IMPLEMENT, ONE AGENT PER STEP
model: per step, from the plan (opus only for hash, encoding or type mapping)   agent: implementer
goal: One step per spawn, one commit per logical change, with tests that would fail on a real
  difference.
returns: COMMITS, TESTS, NEW_TESTS, CHECKSUM_MATCHES
stop: 1, 6

PHASE 4: REVIEW
model: opus   agent: general-purpose
goal: /code-review high on master..HEAD, plus a pass over the key-shape and density matrix, values
  that would now hash the same, and silent truncation. Reproduce each finding before fixing it.
  Report each as fixed, disproved, or accepted with a reason.
returns: FINDINGS_FIXED, FINDINGS_DISPROVED, FINDINGS_ACCEPTED
stop: 1, 6

PHASE 5: TESTS
model: sonnet   agent: test-runner
goal: The full suite with TEST_ACROSS_ALL_DBS=full, then unittest-parallel -j 8 (coordinator JOBs).
  "Testing databases:" must list all five. poetry build and dev/release/smoke_wheel.py on the wheel.
returns: TESTS, DATABASES, WHEEL
stop: 6

PHASE 6: MEASURE
model: sonnet   agent: bench-runner
goal: ab.py --base origin/master --runs 5 on the target pairs and tables (a coordinator JOB),
  and the verdict per row.
returns: RIG, NOISE_PCT, GAIN_PCT_<pair>_<table>
stop: 2, 4

PHASE 7: DOCS, CHANGELOG AND VERSION
model: sonnet   agent: docs-writer
goal: CLAUDE.md (behavior that's easy to get wrong), README (tables with the revision and rig),
  docs/technical-explanation.md, the CHANGELOG section, and the version bump in pyproject.toml,
  data_diff/version.py and the README install URL.
returns: FILES, VERSION
stop: none

PHASE 8: SHIP
model: sonnet   agent: the coordinator, with /ship
goal: Push, open the PR with the results, watch CI (ci-triage on a failure). Then as "How far"
  says: stop at the PR, or STOP 8, then merge with --delete-branch, wait for master CI, tag,
  watch release.yml, and verify the assets and attestation as /ship describes.
returns: PR, CI, RELEASE
stop: 6, 8

FINAL REPORT
The PR and release links, then:
- the rig and the measured noise
- the share of wall time per step, per pair and shape
- the before and after table, with verdicts
- which checksums changed (ideally none)
- review findings by outcome
- what was not tested
Keep what you observed apart from what you assume. End with: run /post-mortem <run-id>.
```
