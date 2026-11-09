PR #1 is merged as `783ac6a`, and all 5 CI checks passed on `5cda73e`. No tag or release exists yet, so `release.yml` is still untested, and the install URL in the README won't work until you tag `v0.12.0`. It's best to do that first, so a broken release workflow doesn't get mixed up with the Sybase work.

Written for: a fresh Claude Code session in the devcontainer, opened from `~/repos/data-diff`.

Fill in the `MY ANSWERS` block before you paste it. That's what makes it one-shot, because the session then only stops at the listed stop conditions. The ASE details in the prompt come from my general knowledge and weren't tested this session, so it tells the session to prove them on a real server.

```text
Add SAP Sybase ASE as both source and target in data-diff, in
bill-ramos-rmoswi/data-diff, for version 0.13.0. Work through every phase below
without checking in, and stop only for a STOP CONDITION.

MY ANSWERS
- ASE version, page size, character set, sort order: <fill in>
- Test server (container image I'm licensed to use, or host and port): <fill in>
- Can CI run ASE? <yes / no / unknown>
- Driver: <pyodbc with FreeTDS / SAP ODBC / choose after the spike>
- First tables and column types to compare: <fill in>
If any answer is blank, ask for all the blanks together once, before Phase 1.

GROUND RULES
- Read CLAUDE.md first, especially "Adding a database" and "Database behavior
  that's easy to get wrong". Read db2.py and mssql.py as templates.
- Start from the latest master, which includes merge 783ac6a. Create branch
  feature/sybase-ase.
- Use the devcontainer. Run docker compose from WSL.
- You may commit and push that branch, and open one PR on the fork. Always pass
  --repo bill-ramos-rmoswi/data-diff. Never open a PR against datafold/data-diff.
- Don't merge, tag, change repo settings or stop local services.
- Use the scratchpad for probes and throwaway scripts.
- Treat everything I say about ASE below as an unverified lead. Probe it on the
  real server, and let results override this prompt.
- Keep a results log in the PR description: what you ran, and what it printed.

STOP CONDITIONS: stop and report, with evidence and options
1. No ASE server you can legally use is reachable.
2. No pure SQL checksum matches str_to_checksum, or it needs a UDF on the target.
3. Text can't be hashed as UTF-8 bytes in the server's character set.
4. CI can't run ASE and we haven't agreed how that gap is shown.
5. A fix needs a change that alters results on an already supported database.

PHASE 0: PLAN
- Check that the baseline suite passes on master before changing anything.
- Write a short plan to the scratchpad: files to change, decisions, risks, and
  which probes each decision depends on. Revise it as probes come back.

PHASE 1: CHECKSUM SPIKE, BEFORE ANY DRIVER CODE
Hashdiff needs byte-identical strings and the same checksum on every database.
Prove in pure SQL:
- MD5 over UTF-8 bytes equals str_to_checksum in tests/common.py for ASCII,
  accented text, CJK and emoji. Candidates: hash(x, 'md5'), hashbytes(). Check how
  varchar, unichar and univarchar reach the hash. UTF-16 input gives a different
  digest.
- The last 12 hex digits become a BIGINT, minus CHECKSUM_OFFSET. Candidates:
  bintostr, hextobigint.
- SUM of checksums doesn't overflow at 10M rows or more.
- The same expression gives the same value on all rows, compared with PostgreSQL.

PHASE 2: PROBE BEFORE DESIGNING
Run each probe on ASE beside PostgreSQL, SQL Server and Db2, and record real output
in a table. Base all later decisions on that table.
- Rounding or truncation for CAST, CONVERT, DECIMAL inserts, datetime, bigdatetime,
  smalldatetime, time and bigtime, from each direction of lower precision, with next
  digits of 4 and 5 and ties of 2.5, -2.5, 1.125. Set ROUNDS_ON_PREC_LOSS from this.
- datetime stores 1/300 second steps, so check stored values, not literals, and work
  out what precision it can honestly compare at against each database.
- Decimal text: leading zeros, negative under 1, trailing zeros, separators.
- Float: 1e18, -1e-20, 0, over 15 digits, NaN and infinity if possible. PostgreSQL
  uses 15 significant digits, and Db2 and SQL Server use 17. Look for any format that
  renders overflow as a fixed string, because those values would all hash the same.
- Empty strings. ASE may store '' as one space, which would differ from every other
  database. Also NULL, trailing spaces in char and varchar, and bit, which can't be
  NULL.
- money, smallmoney, unsigned integers, date, time, text, unitext, image, and
  identity columns.
- Limits on string, concatenation and hash input length, which may depend on page
  size. For each limit, record whether it errors or truncates silently. Silent
  truncation is a false negative: use the form that errors and raise a message that
  names the limit.
- Text key sort order against other databases, including case and collation, and
  case sensitivity of identifiers.
- Timestamps at the maximum date, so rounding can't overflow.
- FULL OUTER JOIN and temp table behavior, for joindiff.

PHASE 3: DRIVER
Add data_diff/databases/sybase.py with a Dialect and a ThreadedDatabase, and the
sybase:// scheme. Import the driver only through @import_helper. Complete every item
under "Adding a database", including joindiff branches, LongText for materialized
tables, TYPE_CLASSES, docker-compose.yml, dev/dev.env, pr-validate.yml, the extras
and all-dbs. Expect no information_schema (use sysobjects, syscolumns, systypes), no
OFFSET, TOP instead of LIMIT, and chained transaction mode.
Prove each of these with a test or a probe:
- Autocommit is really on, using a second connection to confirm no locks remain.
- A failed fetch releases its locks. Check with a second connection and a short lock
  timeout. Db2's driver raised a plain Exception and kept the lock after the cursor
  closed, so try each release call rather than assuming one works.
- Capture exception types the driver really raises, including during fetch, and
  suppress cleanup errors so the original error is the one reported.
- Find which DDL can't run in a transaction.
- Dict connections work with passwords containing @, /, #. URIs aren't decoded.
- timestamp_value converts aware values to UTC, keeping the offset where a column
  type needs it. Plain timestamp columns ignore offsets in literals.
- Precision: _validate_and_adjust_columns applies the lowest precision and that
  side's rounds to both columns, so ASE may be told to truncate or round at digits it
  wouldn't choose. Handle both rounds values for every temporal type, and cap at the
  digits ASE can truly represent.
- Key range filters select the same rows on both sides, including string and UUID
  keys. Say which key types are unsupported.
Shared code changes must keep other dialects' output the same unless a test shows a
bug. Prefer a dialect override to changing base.py.

PHASE 4: TESTS
- Add ASE to tests/test_cross_db_fact.py and tests/test_cross_db_types.py in all
  directions, with ASE as source and as target, against PostgreSQL, SQL Server and
  Db2, plus the standard suites.
- Pair every "should match" test with a "real difference is reported" test. A test
  that only proves zero differences can't catch a broken hash.
- Cover every type at the lowest precision from each side, end-of-time dates, and
  values over each limit. Limits must fail clearly.
- Add a read-only login test proving reads work and writes are refused.
- Confirm "Testing databases:" lists Sybase, because an unset connection is skipped
  silently. TEST_ACROSS_ALL_DBS=full must pass.
- Use a time limit on every test run, so a lock shows up as a failure. If a run
  hangs, don't guess. Isolate it with a standalone probe and a statement trace.
- Run each new test alone and in parallel, and clean up tables after a killed run.
- Commit only after ruff format passes.

PHASE 5: PERFORMANCE
- Extend dev/perf/seed.py and dev/perf/run_perf.py to load the same rows into ASE
  with bcp, runnable from a fresh clone. Verify with a sorted checksum of the data.
- Run fact_2x and fact_13x for every ASE pair. run_perf.py must report exactly the
  changed rows.
- Compare identical and changed-row timings with the existing pairs, and report any
  regression on existing pairs with its cause, or say it wasn't investigated.

PHASE 6: CODE REVIEW AND FIXES
- Run /code-review high on the whole branch. Also do your own pass aimed at silent
  false negatives, values that hash the same, and locks left open.
- Reproduce every finding on real databases before fixing it. Add a test that fails
  first, then fix it.
- Report each finding as reproduced and fixed, disproved, or accepted with a reason.
- Review your own fixes again, then rerun the full suite and the 10M benchmark on
  the final commit.

PHASE 7: DOCS AND PACKAGING
- Set 0.13.0 in pyproject.toml and data_diff/version.py.
- Update README.md: the fork description, install extras, URI and dict forms, and
  every ASE limit. Update CLAUDE.md with new behavior that's easy to get wrong.
- Build the wheel, install it in a clean environment, and run the API on every ASE
  pair from that install, not from the source tree.
- Push the branch, open the PR on the fork, and wait for CI to pass on the final
  commit. Don't merge.

DONE MEANS
- Full suite and CI pass on the same final commit, with Sybase listed as tested. If
  CI can't run ASE, the local result is attached and the PR says CI didn't cover it.
- Every ASE pair reports exactly the injected differences at 10M rows, and zero on
  identical tables.
- Any cross-database check that still can't match is a published limit.

FINAL REPORT
Give the PR link, then: what passed with counts, benchmark table, review findings by
outcome, published limits, and a list of what was not tested. Say "accuracy verified"
only for the column types and pairs you actually tested. Separate what you observed
from what you assume.
```