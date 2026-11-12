---
name: perf
description: Seed the perf tables and time data-diff between database pairs with dev/perf/run_perf.py, then summarize or update the README benchmark table. Use for benchmarks, before/after speed comparisons, or when the user runs /perf.
---

# Benchmarking

Read "Performance tests" in CLAUDE.md first. In short:

- `fact_2x` has 1.55M rows and `fact_13x` has 10.09M.
- `run_perf.py` fails if data-diff doesn't report exactly the rows it changed.
- Timings vary by about 20% from run to run.

## 1. Agree the scope

Unless the user says otherwise, the README is for on-prem → Aurora PostgreSQL: `Db2:PostgreSQL`, `MsSQL:PostgreSQL` and `Sybase:PostgreSQL` at ASE's 2 GB defaults. If the request names neither the pairs nor the tables, ask, because a benchmark of every pair takes about 15 minutes and wasn't wanted last time.

## 2. Check the tables are seeded

```shell
poetry run python dev/probe.py postgres "select count(*) from fact_13x"
poetry run python dev/probe.py sybase "select count(*) from fact_13x" --perf
```

If a count is missing or wrong, seed: `poetry run python dev/perf/seed.py` (about 1 minute), or `--only sybase` for ASE alone.

## 3. Run in the background

```shell
PERF_PAIRS=Db2:PostgreSQL,MsSQL:PostgreSQL,Sybase:PostgreSQL \
  poetry run python dev/perf/run_perf.py fact_2x fact_13x > $SCRATCH/perf.log 2>&1
```

- Run it with `run_in_background` and wait for the notification. Don't poll.
- Options:
  - `PERF_ALGORITHM=rowhash` times `RowHashDiffer`.
  - `SYBASE_CONFIG="..."` labels the ASE settings.
  - `N_THREADS` sets the thread count (default 4).
- The results go to `benchmark_<revision>.jsonl`, which is untracked. Keep it out of commits unless the user asks.

## 4. Compare honestly

- For a before and after comparison, run both on the same machine, at least twice, alternating. A gap under 20% isn't a result until it repeats.
- Report a table: pair, table, scenario (identical, updated, deleted), median seconds, and the change in %.
- Say what the numbers aren't: Db2 Community Edition is limited to 4 cores and 16 GB, and ASE is a trial in a container.
- Before timing a change to checksum SQL, check that it keeps the values: `poetry run python dev/perf/checksum.py fact_13x PostgreSQL,Db2,Sybase,MsSQL` must print one checksum, on master and on the branch. It also times the checksum alone, without bisection.
- When you update the README, change only the pairs in scope, and give the revision the numbers came from.

## Examples

- `/perf Db2, SQL Server and ASE → PostgreSQL at 2 GB, fact_2x and fact_13x, then update the README table.`
- `/perf rowhash vs hashdiff for Sybase:PostgreSQL` → `PERF_ALGORITHM=rowhash PERF_PAIRS=Sybase:PostgreSQL`, twice each.
- `/perf baseline on master, then on this branch, for the checksum change`
