PR #1 is merged as `783ac6a`, and `v0.12.0` is tagged, so the Sybase work starts from a released baseline.

Written for: a fresh Claude Code session in the devcontainer, opened from `~/repos/data-diff`.

The test server is now a known quantity. The facts under TEST SERVER were measured on the image built from [sap-ase-16-trial-docker-image](https://github.com/bill-ramos-rmoswi/sap-ase-16-trial-docker-image) (see its README), so the session can rely on them. ASE SQL behavior (hashing, rounding, empty strings) still comes from general knowledge, and the prompt tells the session to prove it on the server.

Before pasting the prompt, do these once. The session isn't allowed to change package settings, so it can't do them itself:

1. **Image in GHCR.** From a WSL terminal, `docker manifest inspect ghcr.io/bill-ramos-rmoswi/sap-ase:16.1-SP00-PL02` should succeed. If it reports an auth error, log in to `ghcr.io` there (`echo "$GHCR_TOKEN" | docker login ghcr.io -u bill-ramos-rmoswi --password-stdin`, with a token that has `read:packages`). If the tag is missing, run `./scripts/build.sh --push` in the image repo.
2. **CI access.** On GitHub, open the `sap-ase` package → **Package settings → Manage Actions access → Add Repository**, and add `bill-ramos-rmoswi/data-diff` with **Read** or **Write** (done; it has Write). Without it, `GITHUB_TOKEN` can't pull the image and the session hits STOP CONDITION 4.
3. **License date.** The pushed image's grace period ends on Sat 06 Feb 2027. If you rebuild with `--no-cache`, update that date under TEST SERVER.

```text
Add SAP Sybase ASE as both source and target in data-diff, in
bill-ramos-rmoswi/data-diff, for version 0.13.0. Test it the same way as SQL Server
and Db2, including the 10M-row benchmark. Work through every phase below without
checking in, and stop only for a STOP CONDITION.

MY ANSWERS
- ASE version, page size, character set, sort order: 16.1 SP00 PL02 (EBF 31284),
  4k pages, cp850, bin_cp850 (binary, sort order ID 50)
- Test server: the private image ghcr.io/bill-ramos-rmoswi/sap-ase:16.1-SP00-PL02,
  built from the SAP trial. Details under TEST SERVER.
- Can CI run ASE? Yes, for pushes and same-repo PRs, by pulling the private image
  with GITHUB_TOKEN and packages: read. PRs from forks can't pull it and must skip
  the ASE job visibly.
- Driver: choose after the Phase 1 spike. The image ships no ODBC or Python driver,
  and the SAP drivers can't be redistributed to CI, so start with pyodbc + FreeTDS
  (tdsodbc), installed from apt in the devcontainer and in CI. FreeTDS must use
  TDS version 5.0 for ASE.
- First tables and column types to compare: the perf tables fact_2x and fact_13x
  (INT keys, DATE, NUMERIC(19,4), INT), then the standard type suites.

TEST SERVER (measured; these aren't leads)
- Image: ghcr.io/bill-ramos-rmoswi/sap-ase:16.1-SP00-PL02. Rocky Linux 9,
  linux/amd64, about 2.5 GB unpacked (mostly zero-filled devices) and about
  205 MB to pull. Healthy about 6 s after start. Its healthcheck is
  /opt/sap/healthcheck.sh.
- Server name ASE, port 5000, login sa / Password1, database testdb. All devices,
  including master, are in /opt/sap/data. A named volume there (ase-data:/opt/
  sap/data) is filled from the image on first use and keeps everything across
  container recreation; reset it with docker compose down -v. Never bind-mount a
  host directory there, because that hides master. Put new devices in
  /opt/sap/data too: a volume on any other path keeps the device files, but a
  recreated container's master doesn't know about them (tested).
- Client tools in the container: run `source /opt/sap/SYBASE.sh` first; isql and
  bcp are in $SYBASE/OCS-16_1/bin. The devcontainer has no Docker socket and no SAP
  client, so anything seed.py or the tests need must work from the devcontainer
  over TDS (FreeTDS freebcp/tsql, or the Python driver). Use docker exec only from
  WSL, for diagnostics.
- docker stop shuts down cleanly through the entrypoint. Without a volume, a
  restarted container keeps its writable layer, and one that's recreated starts
  from the build-time state.
- Configuration baked into the image (measured; fits mem_limit 2g, idle about
  380 MB; 60 concurrent sessions tested):
    threaded kernel mode, max online engines 4, syb_default_pool 4 threads
    (number of engines at startup isn't supported in threaded mode),
    max memory 786432 2k pages (1.5 GB), default data cache 768 MB,
    procedure cache size 51200 (100 MB), number of user connections 100,
    number of locks 200000, open objects 2000, open indexes 2000,
    tempdb 1.1 GB, testdb 300 MB (250 MB data + 50 MB log,
    trunc log on chkpt, select into/bulkcopy/pllsort on),
    lock scheme allpages, enable unicode conversions 1,
    @@unicharsize 2 (unichar/univarchar store UTF-16), @@ncharsize 1.
  max memory and caches are dynamic. Raising them at runtime needs no restart,
  but the change lives in the container's ASE.cfg, not the volume, so a
  recreated container loses it. testdb is too small for the benchmark.
- Normal log noise: two "SySAM: FlexNet Licensing error:-1,359" lines at each
  start (there's no license file). They aren't failures.
- Character set cp850: varchar/char/text can hold only cp850 characters. CJK and
  emoji fit only in unichar, univarchar or unitext.
- License: the SySAM grace period runs out on Sat 06 Feb 2027 20:46 UTC, and ASE
  then shuts down. The error log prints this line at every start:
  "SySAM: WARNING: ASE will shutdown on ...". If a run is near that date or past
  it, stop (STOP CONDITION 6). Don't try to work around licensing.
- Don't change, rebuild or push the image, and never publish it anywhere public.
  If the image itself must change (for example a utf8 server, or a setting that
  can only be set at build time), stop and report. The fix belongs in the image
  repo. A rebuild that renews the license must use build.sh --no-cache.

GROUND RULES
- Read CLAUDE.md first, especially "Adding a database" and "Database behavior
  that's easy to get wrong". Read db2.py and mssql.py as templates, and
  dev/db2/init.sh with its compose healthcheck as the model for server setup.
- Start from the latest master (v0.12.0 or later). Create branch
  feature/sybase-ase.
- Use the devcontainer. Run docker compose from WSL. The image is private, so WSL
  needs `docker login ghcr.io` once. If the pull fails with an auth error, that's
  STOP CONDITION 1.
- You may commit and push that branch, and open one PR on the fork. Always pass
  --repo bill-ramos-rmoswi/data-diff. Never open a PR against datafold/data-diff.
- Don't merge, tag, change repo or package settings, or stop local services.
- Use the scratchpad for probes and throwaway scripts.
- Treat everything I say about ASE SQL behavior below as an unverified lead. Probe
  it on the real server, and let results override this prompt.
- Keep a results log in the PR description: what you ran, and what it printed.
- Keep the databases in the Docket images as is after running all the tests so that Bill can view the results after testing

STOP CONDITIONS: stop and report, with evidence and options
1. The ASE image can't be pulled or started, or the server isn't reachable from
   the devcontainer.
2. No pure SQL checksum matches str_to_checksum, or it needs a UDF on the target.
3. Text can't be hashed as UTF-8 bytes in pure SQL on this cp850 server (see
   Phase 1). Give the options, including a utf8/bin_utf8 image rebuild.
4. CI can't pull or start the image on a same-repo push.
5. A fix needs a change that alters results on an already supported database.
6. The SySAM grace period has run out, or will before the work can finish.
7. The 10M-row benchmark can't be loaded or run on ASE within the memory budget
   below, even after tuning.

PHASE 0: PLAN AND SERVER SETUP
- Check that the baseline suite passes on master before changing anything.
- Add the ase service to docker-compose.yml under profiles: [ase] (the image
  README has the snippet), with container_name dd-ase, mem_limit
  ${ASE_MEM:-2g} and the named volume ase-data:/opt/sap/data. No perf data
  mount: the seed loads over TDS (Phase 5). Add it to runServices in
  .devcontainer/devcontainer.json only if it doesn't break devcontainers for
  users without GHCR access; otherwise document
  `docker compose --profile ase up -d --wait ase` in CLAUDE.md.
- The image already handles engines, connections, locks and tempdb, so no
  server restart is needed. Runtime setup is only what the tests and benchmark
  decide they need. Keep it in dev/ase/init.sql, idempotent, applied over TDS
  from the devcontainer (seed.py or a test fixture), not via docker exec. That
  covers, for example, a bigger memory and cache locally (dynamic; the 4 GB
  ASE_MEM case), and database options such as "ddl in tran" or a datarows lock
  scheme if Phase 2 shows they're needed.
- Create the perf database (data and log devices) under /opt/sap/data, so it
  lives on the ase-data volume and the 10M-row seed survives container
  recreation. Size it in Phase 5.
- Write a short plan to the scratchpad: files to change, decisions, risks, and
  which probes each decision depends on. Revise it as probes come back.

PHASE 1: CHECKSUM SPIKE, BEFORE ANY DRIVER CODE
Hashdiff needs byte-identical strings and the same checksum on every database.
Prove in pure SQL:
- MD5 over UTF-8 bytes equals str_to_checksum in tests/common.py for ASCII,
  accented text, CJK and emoji. Candidates: hash(x, 'md5'), hashbytes().
  This server is cp850, so:
  - varchar holds cp850 bytes. Accented text hashed directly gives a different
    digest from UTF-8, while plain ASCII matches. Find a server-side conversion
    to UTF-8 bytes, or show there isn't one.
  - univarchar/unitext hold UTF-16 (@@unicharsize 2), which also gives a
    different digest. Check whether convert(), char/unichar functions or
    "enable unicode conversions" can produce UTF-8 bytes server-side.
  - The client connection charset changes what the client sees, not what the
    server hashes, so it doesn't fix this.
  - If only ASCII can match, measure it and decide between "ASCII-only text
    columns are a published limit" and STOP CONDITION 3, with evidence.
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
  The perf tables use money in the SQL Server source and NUMERIC(19,4) elsewhere,
  so check money and numeric(19,4) render alike.
- Float: 1e18, -1e-20, 0, over 15 digits, NaN and infinity if possible. PostgreSQL
  uses 15 significant digits, and Db2 and SQL Server use 17. Look for any format that
  renders overflow as a fixed string, because those values would all hash the same.
- Empty strings. ASE may store '' as one space, which would differ from every other
  database. Also NULL, trailing spaces in char and varchar, and bit, which can't be
  NULL.
- money, smallmoney, unsigned integers, date, time, text, unitext, image, and
  identity columns.
- Limits on string, concatenation and hash input length. With 4k pages, check the
  varchar and row-size limits that come from that. For each limit, record whether it
  errors or truncates silently. Silent truncation is a false negative: use the form
  that errors and raise a message that names the limit.
- Text key sort order against other databases. bin_cp850 is binary, so check it
  against PostgreSQL's collation and SQL Server's, including case, accented
  characters (cp850 byte order isn't Unicode code-point order) and case
  sensitivity of identifiers.
- Timestamps at the maximum date, so rounding can't overflow.
- FULL OUTER JOIN and temp table behavior, for joindiff. tempdb is 1.1 GB.
- Lock scheme: the server default is allpages. Check whether test tables need
  datarows to avoid lock waits in parallel runs.

PHASE 3: DRIVER
Add data_diff/databases/sybase.py with a Dialect and a ThreadedDatabase, and the
sybase:// scheme. Import the driver only through @import_helper. Complete every item
under "Adding a database", including joindiff branches, LongText for materialized
tables, TYPE_CLASSES, docker-compose.yml, dev/dev.env, pr-validate.yml, the extras
and all-dbs. Install the FreeTDS ODBC driver in the devcontainer image and in CI.
Expect no information_schema (use sysobjects, syscolumns, systypes), no OFFSET,
TOP instead of LIMIT, and chained transaction mode.
Prove each of these with a test or a probe:
- Autocommit is really on, using a second connection to confirm no locks remain.
- A failed fetch releases its locks. Check with a second connection and a short lock
  timeout. Db2's driver raised a plain Exception and kept the lock after the cursor
  closed, so try each release call rather than assuming one works.
- Capture exception types the driver really raises, including during fetch, and
  suppress cleanup errors so the original error is the one reported.
- Find which DDL can't run in a transaction. ASE refuses some DDL in a transaction
  unless "ddl in tran" is on for the database. Decide whether init.sql turns it on
  or the driver avoids it.
- Connection charset: say which client charset the driver uses (FreeTDS
  client charset) and prove values round-trip unchanged for cp850 text.
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
- Add TEST_SYBASE_CONN_STRING to tests/common.py and DATADIFF_SYBASE_URI to
  dev/dev.env and the devcontainer, pointing at the compose service name.
- Add ASE to tests/test_cross_db_fact.py and tests/test_cross_db_types.py in all
  directions, with ASE as source and as target, against PostgreSQL, SQL Server and
  Db2, plus the standard suites.
- Pair every "should match" test with a "real difference is reported" test. A test
  that only proves zero differences can't catch a broken hash.
- Cover every type at the lowest precision from each side, end-of-time dates, and
  values over each limit. Limits must fail clearly. Cover accented text in
  varchar, and CJK and emoji in univarchar, with whatever Phase 1 decided.
- Add a read-only login test proving reads work and writes are refused.
- Confirm "Testing databases:" lists Sybase, because an unset connection is skipped
  silently. TEST_ACROSS_ALL_DBS=full must pass.
- Use a time limit on every test run, so a lock shows up as a failure. If a run
  hangs, don't guess. Isolate it with a standalone probe and a statement trace
  (sp_who, sp_lock and the error log through docker logs dd-ase from WSL).
- Run each new test alone and in parallel, and clean up tables after a killed run.
- CI: in .github/workflows/pr-validate.yml, add ASE to the integration job, or a
  sibling job, with permissions packages: read, a GHCR login using GITHUB_TOKEN,
  `docker compose --profile ase up -d --wait`, and a memory split that fits the
  16 GB runner. The integration job already runs Db2 6g, SQL Server 3g,
  PostgreSQL 2g and MySQL 2g, so adding ASE 2g (which the image's settings fit)
  makes 15g. Prefer a sibling job for the ASE pairs if that leaves too little
  for the runner itself. CI needs no ase-data volume. Skip it for
  fork PRs with
  if: github.event_name != 'pull_request' ||
      github.event.pull_request.head.repo.full_name == github.repository
  and make the skip visible in the job summary. Show `docker compose logs ase` on
  failure.
- Commit only after ruff format passes.

PHASE 5: PERFORMANCE (same method as SQL Server and Db2)
- Extend dev/perf/seed.py to load the same CSVs (dev/perf/data/fact_2x.csv and
  fact_13x.csv, exported from SQL Server) into ASE, runnable from a fresh clone
  in the devcontainer. ASE has no server-side file load like Db2's LOAD, and the
  devcontainer can't docker exec, so load over TDS: freebcp from FreeTDS is the
  first candidate. Use the same schema as Db2, with UnitCost NUMERIC(19,4), and
  add the primary key (ScaleKey, ProductKey, DateKey) after the load, so bcp
  stays minimally logged. Verify with a sorted checksum of the data, compared
  with PostgreSQL.
- Size first: fact_13x is 10.09M rows. Estimate it from fact_2x's measured
  sp_spaceused (data + index), then make the perf database and its log large
  enough for both tables, the clustered index build and run_perf.py's
  save/restore of changed rows. Record the final sizes in CLAUDE.md.
- Extend run_perf.py PAIRS with ASE as source and as target against PostgreSQL,
  SQL Server and Db2. run_perf.py must report exactly the changed rows.
- Run fact_2x and fact_13x for every pair, and write benchmark_<revision>.jsonl as
  for the other databases.
- Compare identical and changed-row timings with the existing pairs, and report any
  regression on existing pairs with its cause, or say it wasn't investigated.
- Run the benchmark with ASE_MEM=4g and the cache raised to match, and also
  once at the 2g image defaults. Report the ASE configuration used (engines,
  memory, cache, lock scheme) beside the numbers. Like Db2 Community Edition's 4 cores and 16 GB, a trial ASE in a
  container isn't a production number.

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
- Update README.md: the fork description, install extras, URI and dict forms, the
  FreeTDS requirement, and every ASE limit (including the cp850 text result from
  Phase 1). Update CLAUDE.md with new behavior that's easy to get wrong, the ase
  compose profile, the private image and how to get access, and the license
  expiry and rebuild (see the image repo README).
- Build the wheel, install it in a clean environment, and run the API on every ASE
  pair from that install, not from the source tree.
- Push the branch, open the PR on the fork, and wait for CI to pass on the final
  commit. Don't merge.

DONE MEANS
- Full suite and CI pass on the same final commit, with Sybase listed as tested
  in the CI log of a same-repo push. The PR says fork PRs skip ASE.
- Every ASE pair, in both directions, reports exactly the injected differences at
  10M rows, and zero on identical tables.
- Any cross-database check that still can't match is a published limit.

FINAL REPORT
Give the PR link, then: what passed with counts, benchmark table (with the ASE
configuration), review findings by outcome, published limits, the days left on the
SySAM grace period, and a list of what was not tested. Say "accuracy verified" only
for the column types and pairs you actually tested. Separate what you observed from
what you assume.
```
