Written for: you, to check MY ANSWERS, then one Sonnet session that runs it with `/orchestrate`.

This plan takes on the follow-ups left by run `20261113-tuning` ([postmortem-tuning-2026-11-13.md](postmortem-tuning-2026-11-13.md)), and releases them. It's built from [Orchestrated-Plan-Template.md](Orchestrated-Plan-Template.md). The CHANGELOG's Unreleased section already holds two CLI fixes and the release hardening, so this release ships those too.

Before you start:

1. In WSL, start the databases on the matched rig:
   ```shell
   docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --wait postgres mysql mssql db2
   docker compose -f docker-compose.yml -f docker-compose.bench.yml --profile ase up -d --wait ase
   ```
2. In the devcontainer, run `poetry run python dev/bench/rig.py apply`, then the Db2 restart it prints, in WSL.
3. Turn on Remote Control in Claude Code, so that the release STOP reaches your phone.
4. Start a session on Sonnet, and run `/orchestrate docs/plan/Followups-Orchestrated-Prompt.md`. Watch it with `bash dev/status.sh` or `/status`.
5. When it's done, run `/post-mortem <run-id>`.

```text
Close the follow-ups from run 20261113-tuning in bill-ramos-rmoswi/data-diff, and release them,
without changing any checksum value a supported pair relies on.

MY ANSWERS
- Pairs that matter: Db2 -> PostgreSQL, SQL Server -> PostgreSQL, ASE -> PostgreSQL.
  All other pairs must still pass.
- Tables: fact_2x and fact_13x (dev/perf/seed.py), oltp_order_line (dev/perf/seed_oltp.py).
- Settings: matched rig, N_THREADS=4, hashdiff at the differ's default thread pool.
- Scope, in order:
  F1. SQL Server `numeric`: map it in MsSQL's TYPE_CLASSES like `decimal`. Today it parses as
      UnknownColType and is hashed as NVARCHAR(MAX) text, so oltp_order_line never takes the
      bounded hash input. Probe first: does the mapped `numeric` give the same checksum as
      PostgreSQL, Db2 and ASE for the same values (precision, scale, negatives under 1, NULL)?
      Does the oltp checksum change? If the unmapped form already matched only by luck and the
      mapped form fixes a real mismatch, that's a bug fix (say so). Otherwise a changed value is
      STOP 1.
  F2. The '|' blind spot, in the leaf only: Db2 and PostgreSQL leaf row hashes (LEAF_ROW_HASH)
      must tell 'a|b','c' from 'a','b|c'. For example, add per-column lengths to the leaf's hash
      input or NULL mask. Checksums and RowHashDiffer stay as they are, and CLAUDE.md says the
      blind spot remains there.
  F3. Re-run fact_2x, fact_13x and oltp_order_line on the release head against v0.14.0's
      revision (7f4ad77), and refresh the README table with the revision and the rig.
- Out of scope: mixed-case text UUID key ranges between Db2 and PostgreSQL (false positives),
  the stats counter race, pydantic 2 support.
- Done means: F1 and F2 merged with tests that would fail without them, F3's table in the README,
  and the release published and verified.

PRE-DECIDED  (the coordinator applies these without asking)
- Noise: per row. A row's change counts only beyond 2x its own A/A noise. STOP 2 only when the
  rig isn't bench or the median noise of the target rows is over 15%.
- Public defaults that may change: none. F1 changes how SQL Server `numeric` columns are typed,
  which is a fix, not a default.
- Checksum values: may not change, except where phase 2 shows the old value was a mismatch
  (a bug), which then goes in the CHANGELOG's Fixed and Compatibility sections.
- Version: SemVer. 0.15.1 if nothing visible changes beyond fixes. 0.16.0 if F1 changes a
  checksum value or a detected type users can see. The bump and the CHANGELOG section (moving
  Unreleased under the new version) go in the PR.
- How far: through release, with one STOP (8) before the merge.
- Test-only fixes for a test's own misuse: STOP, with the fix ready.

GROUND RULES
- Read CLAUDE.md first, especially "Long jobs", "Cross-database matching", "Leaf row hash",
  "SQL Server hash input" and "Performance tests".
- Branch fix/followups-tuning from the latest master.
- Probe with dev/probe.py, dev/perf/checksum.py and dev/perf/profile_diff.py. Estimate from the
  SQL data-diff generates on the seeded tables (LOG_LEVEL=DEBUG), with the detected types.
- Keep files under .runs/<run-id>/, not in the repo.
- Benchmarks only on the matched rig: dev/bench/rig.py check must say "Rig: bench".
- F2's tests cover the key shapes and densities in /cross-db-design, and pair each "should match"
  with a "difference is reported".
- Prefer dialect overrides to changes in base.py.
- Don't stop services or run rig.py apply. Merge and tag only after STOP 8's go-ahead.
- End commit messages with the attribution line from the session.

STOP CONDITIONS: stop and report, with evidence and options
1. A checksum for a supported pair changes, and phase 2 hasn't shown the old value was a bug.
2. The rig isn't "bench", or the median A/A noise of the target rows is over 15%.
5. A change needs a server setting, UDF or image change on the target.
6. Any test fails, run_perf.py reports wrong rows, or the wheel smoke test fails.
7. A decision PRE-DECIDED doesn't cover.
8. Release: the PR's CI is green. Ask once to merge and release.

PHASE 0: RIG, SEED AND NOISE
model: sonnet   agent: bench-runner
goal: rig.py check. Counts for fact_2x, fact_13x and oltp_order_line on all four databases.
  A/A (ab.py --base HEAD --runs 5) on the three target pairs and all three tables, as a JOB.
returns: RIG, NOISE_PCT, NOISY_ROWS
stop: 2

PHASE 1: DESIGN F1 AND F2
model: opus   agent: designer
goal:
  F1: the detected type of every oltp_order_line column on SQL Server before and after the
  mapping, and the checksum of the table and of each numeric column on all four databases, before
  and after. The /cross-db-design table for `numeric` (precision 38, scale 0 to 10, negatives under
  1, NULL). Expected gain on MsSQL -> PostgreSQL oltp, from the bounded hash's share.
  F2: the leaf hash or mask shape for Db2 and PostgreSQL, its SQL on both, the probe that shows
  'a|b','c' against 'a','b|c' now differs, and its cost on a leaf (dense scenario).
  Numbered steps with model and tests. The version and the CHANGELOG lines.
returns: CHECKSUMS_CHANGE, F1_TYPES_CHANGED, EXPECTED_GAIN_PCT_MsSQL_oltp, STEPS, VERSION, PLAN
stop: 1, 5, 7

PHASE 2: IMPLEMENT, ONE AGENT PER STEP
model: opus for F1 (type mapping feeds the hash) and F2 (leaf hash input); sonnet for any step
  the plan marks otherwise   agent: implementer
goal: One step per spawn, one commit per logical change.
returns: COMMITS, TESTS, NEW_TESTS, CHECKSUM_MATCHES
stop: 1, 6

PHASE 3: REVIEW
model: opus   agent: general-purpose
goal: /code-review high on master..HEAD, plus a pass over the key-shape and density matrix, values
  that would now hash the same, and silent truncation. Reproduce each finding before fixing it.
returns: FINDINGS_FIXED, FINDINGS_DISPROVED, FINDINGS_ACCEPTED
stop: 1, 6

PHASE 4: TESTS
model: sonnet   agent: test-runner
goal: The full suite with TEST_ACROSS_ALL_DBS=full, then unittest-parallel -j 8 (coordinator
  JOBs). "Testing databases:" lists all five. poetry build, then dev/release/smoke_wheel.py.
returns: TESTS, DATABASES, WHEEL
stop: 6

PHASE 5: MEASURE (F3)
model: sonnet   agent: bench-runner
goal: ab.py --base 7f4ad77 --runs 5 on the three target pairs and all three tables (a coordinator
  JOB), and the verdict per row.
returns: RIG, NOISE_PCT, GAIN_PCT_<pair>_<table>
stop: 2, 6

PHASE 6: DOCS, CHANGELOG AND VERSION
model: sonnet   agent: docs-writer
goal: CLAUDE.md ("SQL Server hash input" for numeric, "Leaf row hash" for the closed blind spot),
  the README performance table from phase 5, the CHANGELOG (Unreleased moved under the version
  from phase 1, with F1 and F2 added), and the version in pyproject.toml, data_diff/version.py and
  the README install URL.
returns: FILES, VERSION
stop: none

PHASE 7: SHIP AND RELEASE
model: sonnet   agent: the coordinator, with /ship
goal: Push, open the PR with the results, watch CI (ci-triage on a failure). STOP 8 with the
  version, the CHANGELOG section, the A/B table and CI. On the go-ahead: merge with
  --delete-branch, wait for master CI, tag v<version>, watch release.yml, then verify:
  the assets (wheel, sdist, SHA256SUMS, sbom.spdx.json), sha256sum -c, gh attestation verify,
  and smoke_wheel.py on the downloaded wheel. This is the first release with SBOM and
  attestation steps, so a failure there is STOP 6 with the job's log via ci-triage.
returns: PR, CI, RELEASE, VERIFIED
stop: 6, 8

FINAL REPORT
The PR and release links, then:
- the rig and the measured noise
- F1: detected types before and after, checksums before and after, the oltp gain
- F2: the shape chosen, the probe that now differs, its cost
- the before and after table against 7f4ad77, with verdicts
- which checksums changed (ideally none)
- review findings by outcome
- the release checks
- what was not tested
Keep what you observed apart from what you assume. End with: run /post-mortem <run-id>.
```
