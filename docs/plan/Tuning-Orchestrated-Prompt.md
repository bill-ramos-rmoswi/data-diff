Written for: you, to fill in MY ANSWERS, then a single Claude Code session in the devcontainer, which runs it with `/orchestrate`.

This plan takes on the speed-ups that [technical-explanation.md](../technical-explanation.md) lists as not implemented yet, for data-warehouse and OLTP-shaped tables. It applies what [postmortem-fewer-casts-2026-11-12.md](postmortem-fewer-casts-2026-11-12.md) learned:

- One coordinator session runs every phase as a background agent on the model that phase names.
- You're interrupted only at a STOP, or when it's done.
- Benchmarks run on the matched rig.
- No code is written for a gain that can't be measured.

Before you start:

1. In WSL, start the databases on the matched rig:
   ```shell
   docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --wait postgres mysql mssql db2
   docker compose -f docker-compose.yml -f docker-compose.bench.yml --profile ase up -d --wait ase
   ```
2. In the devcontainer, apply the rig settings, and run any restart it prints in WSL: `poetry run python dev/bench/rig.py apply`.
3. Fill in MY ANSWERS below.
4. Start a session on Sonnet, and run `/orchestrate docs/plan/Tuning-Orchestrated-Prompt.md`.
5. To watch it, run `bash dev/status.sh` in any terminal, or `/status`.

```text
Make data-diff's cross-database diffs faster for DW and OLTP tables, in
bill-ramos-rmoswi/data-diff, without changing any checksum value a supported
pair relies on, unless every dialect changes together in one release.

MY ANSWERS  (fill in or keep the defaults)
- Pairs that matter: Db2 → PostgreSQL, SQL Server → PostgreSQL, ASE →
  PostgreSQL (on-prem to Aurora). All other pairs must still pass.
- DW tables: fact_2x (1.55M rows) and fact_13x (10.09M), from dev/perf/seed.py.
- OLTP tables: <design one in phase 1, or name a source. Default: an
  AdventureWorks-style order line, about 2M rows, wide rows with varchar,
  a uuid or composite key, timestamps and NULLs>
- Settings: matched rig (4 vCPU / 4 GB each), N_THREADS=4, hashdiff.
  rowhash only where a change also affects it.
- Candidates, in my order of interest (technical-explanation.md):
  1. bisection tuning: dynamic bisection_factor, bisection_threshold, threads,
     and large key gaps
  2. a faster hash than MD5, where every dialect can compute it identically
  3. bypassing Python and driver limits when downloading many rows
  4. per-engine settings data-diff can ask for on its own connections
     (for example SQL Server MAXDOP hints), never server settings
- Out of scope: <anything>
- Done means: a gain beyond 2x the measured noise on at least one target
  pair and table shape, or evidence of where the time goes and why no
  candidate clears the noise.

GROUND RULES
- Read CLAUDE.md first, especially "Long jobs", "Cross-database matching",
  "Database behavior that's easy to get wrong" and "Performance tests".
- Branch perf/tuning from the latest master.
- Probe with dev/probe.py, dev/perf/checksum.py and dev/perf/profile_diff.py.
  Keep files under .runs/<run-id>/, not in the repo.
- Benchmarks only on the matched rig: dev/bench/rig.py check must say
  "Rig: bench" before any timing that's compared.
- You may commit and push the branch, and open one PR on the fork with
  --repo bill-ramos-rmoswi/data-diff. Don't merge, tag, stop services, or
  run rig.py apply.
- Prefer dialect overrides to changes in base.py.
- End commit messages with the attribution line from the session.

STOP CONDITIONS: stop and report, with evidence and options
1. A checksum for a supported pair stops matching, and the only fix changes
   values on a dialect that isn't part of the change.
2. The rig isn't "bench", or phase 0's A/A noise is over 15% on a target pair.
3. Amdahl: for every candidate, the step's share of wall time times its
   expected speed-up is under 2x the measured noise, on every target pair.
4. After phase 5, no target pair shows a gain beyond 2x the noise.
5. A change needs a server setting, UDF or image change on the target.
6. Any test fails, or run_perf.py reports wrong rows.

PHASE 0: RIG AND BASELINE
model: sonnet   agent: bench-runner
goal: Confirm the rig, measure the noise, and baseline master.
- rig.py check --noise 5. Then ab.py --base HEAD --runs 5 (A/A) on the
  three target pairs, on fact_2x and fact_13x. That's the coordinator's job.
outputs: rig settings per engine, A/A table with noise per row
returns: RIG, NOISE_PCT, BASELINE_<pair>_<table>
stop: 2

PHASE 1: WHERE THE TIME GOES
model: sonnet   agent: prober
goal: For each target pair and both table shapes, where a diff's wall time goes.
- profile_diff.py for identical and updated, on fact_13x and the OLTP table.
- Design the OLTP table if MY ANSWERS doesn't name one, and return its seed
  as a JOB for the coordinator. Seed it into all four databases, the same way
  dev/perf/seed.py does, with a check of counts and checksums.
- For bisection: count queries, segments and downloaded rows per diff, at the
  default bisection_factor and threshold and at two other settings each.
outputs: share of wall time per step, per pair and table shape
returns: SHARE_<step>_<pair>_<shape> for checksum, key range, download, client
stop: none

PHASE 2: DESIGN
model: opus   agent: designer
goal: A plan for the candidates that clear the Amdahl gate.
- For each candidate, the /cross-db-design table, the expected end-to-end
  gain with its arithmetic, and whether checksums change.
outputs: a plan file under .runs/<run-id>/
returns: EXPECTED_GAIN_PCT_<pair>, CHECKSUMS_CHANGE, PLAN
stop: 1, 3, 5

PHASE 3: IMPLEMENT
model: sonnet (opus if the plan touches hashing or encodings)   agent: implementer
goal: Implement the plan, one commit per logical change.
returns: COMMITS, TESTS, CHECKSUM_MATCHES
stop: 1, 6

PHASE 4: TESTS
model: sonnet   agent: test-runner
goal: The full suite with TEST_ACROSS_ALL_DBS=full, then unittest-parallel -j 8.
  Check that "Testing databases:" lists all five. Both are coordinator jobs.
returns: TESTS, DATABASES
stop: 6

PHASE 5: MEASURE
model: sonnet   agent: bench-runner
goal: ab.py --base origin/master --runs 5 on the target pairs and both table
  shapes, run by the coordinator, and the verdict per row.
returns: RIG, NOISE_PCT, GAIN_PCT_<pair>_<table>
stop: 2, 4

PHASE 6: REVIEW
model: opus   agent: general-purpose
goal: /code-review high on master..HEAD, plus a pass for values that would
  now hash the same and for silent truncation. Reproduce each finding with a
  probe or a failing test before fixing it, and report it as fixed,
  disproved, or accepted with a reason.
returns: FINDINGS_FIXED, FINDINGS_DISPROVED, FINDINGS_ACCEPTED
stop: 1, 6

PHASE 7: SHIP
model: sonnet   agent: docs-writer, then the coordinator with /ship
goal: Update CLAUDE.md (new behavior that's easy to get wrong), the README
  benchmark table (target pairs, the revision and the rig), and
  technical-explanation.md (what is now implemented). Push, open the PR with
  the results, and watch CI. Use ci-triage (haiku) on a failure. Don't merge.
returns: PR, CI
stop: 6

FINAL REPORT
The PR link, then:
- the rig and the measured noise
- the share of wall time per step from phase 1, per pair and shape
- the before and after table, with verdicts
- which checksums changed (ideally none)
- review findings by outcome
- what was not tested
Keep what you observed apart from what you assume.
```
