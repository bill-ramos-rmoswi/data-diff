---
name: test
description: Run the smallest set of data-diff tests that covers a change, in the background, and report only the summary and failures. Use after editing code, before committing, or when the user runs /test or asks to "run the tests".
---

# Running tests

Tests use `unittest`, not pytest. Full output can run to thousands of lines, so keep it out of the conversation.

## 1. Pick the smallest command

| Changed | Run |
| --- | --- |
| One dialect, for example `databases/sybase.py` | `-k Sybase -k sybase` (or `-k Db2`, `-k MsSQL`, `-k PostgreSQL`) |
| `normalize_*`, `md5_as_int`, `render_checksum`, `base.py` | `tests.test_cross_db_types tests.test_cross_db_fact tests.test_database_types`, with `TEST_ACROSS_ALL_DBS=full` |
| Digests, LOBs, `rowhash_tables.py` | `tests.test_rowhash` plus the `LargeValue` classes: `-k LargeValue` |
| `queries/` | `tests.test_query tests.test_sql` |
| hashdiff or joindiff | `tests.test_diff_tables tests.test_joindiff` |
| Before a commit or PR | everything: `poetry run unittest-parallel -j 8` |

## 2. Run it

```shell
timeout 1800 poetry run python -m unittest <modules or -k ...> > $SCRATCH/test.log 2>&1; echo "exit $?" >> $SCRATCH/test.log
```

- `$SCRATCH` is the session scratchpad. Run anything longer than a minute with `run_in_background`, and wait for its notification. Don't `sleep`.
- The `timeout` makes a lock show up as a failure. If it fires, don't guess: find the hang with `/db-probe` and the lock views (`sp_who`/`sp_lock` on ASE, `MON_GET_LOCKS` on Db2).
- If ASE isn't running, prefix with `env -u DATADIFF_SYBASE_URI` and say that ASE wasn't tested.
- For a large run, hand it to the `test-runner` agent, which returns only the failures.

## 3. Report

```shell
grep -m1 "Testing databases" $SCRATCH/test.log
tail -5 $SCRATCH/test.log
grep -E "^(FAIL|ERROR):" $SCRATCH/test.log | head -20
```

- Check that `Testing databases:` lists every database the change touches. A database that isn't configured is skipped silently, which looks like a pass. Only modules that import `tests/common.py` print this line. `tests.test_query`, for example, doesn't touch a database.
- Report the pass count, the failures with one line of cause each, and which databases weren't tested. Read a full traceback only for the failures you'll fix.

## Examples

- `/test I changed normalize_digest in sybase.py. Run the smallest suite that covers it.` → `-k Sybase -k LargeValue`
- `/test full run, without ASE` → `env -u DATADIFF_SYBASE_URI poetry run unittest-parallel -j 8`
- `/test TEST_ACROSS_ALL_DBS=full for the checksum change`
