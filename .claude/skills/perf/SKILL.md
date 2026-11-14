---
name: perf
description: Seed the perf tables and time data-diff between database pairs with dev/perf/run_perf.py, then summarize or update the README benchmark table. Use for benchmarks, before/after speed comparisons, or when the user runs /perf.
---

# Benchmarking

Read "Performance tests" in CLAUDE.md first. In short:

- `fact_2x` has 1.55M rows and `fact_13x` has 10.09M.
- `oltp_order_line` has 2M rows (`dev/perf/seed_oltp.py`).
- `run_perf.py` fails if data-diff doesn't report exactly the rows it changed.
- Noise differs by row. On the matched rig, the A/A median was 5%, and short diffs (under 2 s) reached 20–50%. Use the noise `ab.py` measures for each row, not a fixed figure.

## 1. Agree the scope

Unless the user says otherwise, the README is for on-prem → Aurora PostgreSQL: `Db2:PostgreSQL`, `MsSQL:PostgreSQL` and `Sybase:PostgreSQL` at ASE's 2 GB defaults. If the request names neither the pairs nor the tables, ask, because a benchmark of every pair takes about 15 minutes and wasn't wanted last time.

## 2. Check the tables are seeded

```shell
poetry run python dev/probe.py postgres "select count(*) from fact_13x"
poetry run python dev/probe.py sybase --perf "select count(*) from fact_13x"
```

If a count is missing or wrong, seed: `poetry run python dev/perf/seed.py` (about 1 minute), or `--only sybase` for ASE alone.

## 3. Check the rig

Numbers are comparable only on the matched rig: `docker-compose.bench.yml`, 4 vCPU and 4 GB per database. Without it, SQL Server ran 16 workers on a 4-CPU quota, which made its numbers up to 3 times too slow.

```shell
poetry run python dev/bench/rig.py check           # about 10 s. Must end with "Rig: bench"
```

If it doesn't, give the user the WSL commands from the top of `docker-compose.bench.yml`, then run `rig.py apply`, which prints a Db2 restart to run in WSL.

To measure the noise, run `rig.py check --noise 5` in the background. It takes several minutes, and was once killed in the foreground at 115 s.

## 4. Run in the background

A comparison of two revisions:

```shell
PERF_PAIRS=Db2:PostgreSQL,MsSQL:PostgreSQL,Sybase:PostgreSQL \
  poetry run python dev/perf/ab.py --base origin/master --runs 5 fact_2x fact_13x > $SCRATCH/ab.log 2>&1
```

A single run: `poetry run python dev/perf/run_perf.py fact_2x fact_13x`, with the same `PERF_PAIRS`.

- **Running it.** Use `run_in_background` with the command itself, never `nohup` or `&`, and wait for the notification. Don't poll.
- **Saying what's running.** Tell the user what is running, how long it should take (about 10 minutes per run for those three pairs on both tables), and that `bash dev/status.sh` shows its `[i/N]` progress.
- **Options.** `PERF_ALGORITHM=rowhash` times `RowHashDiffer`, `SYBASE_CONFIG="..."` labels the ASE settings, and `N_THREADS` sets the thread count (default 4).
- **Before optimizing a step,** run `dev/perf/profile_diff.py` once on the pair. It shows that step's share of the wall time, which caps the gain.
- **Results.** They go to `benchmark_<revision>.jsonl`, or to `ab.py`'s `--out` directory, and each row carries the rig from `.runs/rig.json`. These files are untracked, so keep them out of commits unless the user asks.

## 5. Compare honestly

- **The noise rule.** Use `ab.py`'s verdict: a change counts only if it's more than twice the noise measured in the same comparison. A fixed 20% doesn't count, and neither does a single run.
- **An A/A run** (`--base HEAD`) measures the noise alone. Run one when the rig changed, and for each table shape in scope, including a table seeded since the last A/A.
- **Profile at the defaults users get.** `profile_diff.py` and `run_perf.py` take the thread pool from `--max-threadpool-size` and `PERF_POOL`. Check that it matches what `diff_tables()` and the CLI would use before reading a profile.
- **Report a table:** pair, table, scenario (identical, updated, deleted), median seconds, change in %, noise and verdict.
- **Say what the numbers aren't.** Db2 Community Edition is limited to 4 cores and 16 GB, ASE is a trial in a container, and WSL's vCPUs move between P and E cores.
- **README updates.** Change only the pairs in scope, and give the revision and the rig the numbers came from.

## Examples

- `/perf Db2, SQL Server and ASE → PostgreSQL at 2 GB, fact_2x and fact_13x, then update the README table.`
- `/perf rowhash vs hashdiff for Sybase:PostgreSQL` → `PERF_ALGORITHM=rowhash PERF_PAIRS=Sybase:PostgreSQL`, twice each.
- `/perf baseline on master, then on this branch, for the checksum change`
