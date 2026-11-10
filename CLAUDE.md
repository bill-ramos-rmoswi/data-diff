# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status and current goal

Upstream Datafold stopped developing open source data-diff in May 2024, so this fork (`bill-ramos-rmoswi/data-diff`) owns its CI and releases. Don't open PRs against `datafold/data-diff`; pass `--repo bill-ramos-rmoswi/data-diff` to `gh`.

Goal: use the Python API to validate data between IBM Db2 12.1, SQL Server 2025 (latest CU), PostgreSQL 18.6 and SAP ASE 16.1, deployed as a wheel attached to a GitHub Release.

Pinned targets are in `docker-compose.yml`: `db2:12.1.5.0`, `mssql/server:2025-CU9-GDR1-ubuntu-24.04`, `postgres:18.6` and `ghcr.io/bill-ramos-rmoswi/sap-ase:16.1-SP00-PL02`. MySQL stays in the stack because `test_api.py`, `test_sql.py` and `test_main.py` hardcode it.

## Environment

Work inside the devcontainer, opened from the WSL clone at `~/repos/data-diff`: Python 3.11, Poetry 1.8.5, ODBC Driver 18, `sqlcmd`/`bcp` and `psql` are preinstalled, and `DATADIFF_*_URI` is set to compose service names.

The container has no Docker socket. Start, stop and reset databases from a WSL terminal:

```shell
docker compose up -d --wait postgres mysql mssql db2     # Db2 takes a minute or more when its volume is new
docker compose --profile ase up -d --wait ase            # SAP ASE, from a private image (see below)
docker compose --profile legacy up -d                    # stale databases: presto, trino, clickhouse, vertica
docker compose down -v                                   # delete all database volumes
```

From outside the compose network, SQL Server is on host port **14330**, because a local instance may hold 1433. Db2 needs `dev/db2/init.sh`, which raises the transaction log limits, and its healthcheck waits for that.

### SAP ASE

- **Private image.** `ghcr.io/bill-ramos-rmoswi/sap-ase:16.1-SP00-PL02` is built from the SAP trial by [sap-ase-16-trial-docker-image](https://github.com/bill-ramos-rmoswi/sap-ase-16-trial-docker-image), and must never be published. To pull it, run `docker login ghcr.io` once in WSL with a token that has `read:packages`. CI pulls it with `GITHUB_TOKEN`, because the package grants this repo access. Pull requests from forks can't, so they skip the ASE job, and `sybase-skipped` says so in the job summary.
- **It isn't in `runServices`**, so a devcontainer without GHCR access still starts. The devcontainer sets `DATADIFF_SYBASE_URI` anyway. When ASE isn't running, `tests/common.py` fails at import and says to start it, or to unset the variable.
- **License.** The SySAM grace period ends on **Sat 06 Feb 2027 20:46 UTC**, and ASE then shuts down. `sp_lmconfig` shows the date, and so does the error log at each start. A rebuild with `scripts/build.sh --no-cache` in the image repo renews it.
- **Data** is in the named volume `ase-data` at `/opt/sap/data`, which holds every device, `master` included. Never bind-mount it, and put new devices there, or a recreated container won't know them. Server ASE, port 5000, `sa`/`Password1`, database `testdb`.
- **Runtime setup** is `dev/ase/init.sql`, which `dev/ase/init.py` applies over TDS. `tests/common.py` runs it on every test run, because it's idempotent. It turns on `allow nulls by default` in `testdb`, raises the procedure cache to 120000 pages, and creates the read-only login `dd_reader`. The procedure cache setting lives in the container's `ASE.cfg`, not the volume, so a recreated container needs init again.
- **Memory.** `mem_limit` is `${ASE_MEM:-2g}`, which the image's settings fit (max memory 1.5 GB, data cache 768 MB). To try more, recreate it with `ASE_MEM=4g` and raise `max memory` and the data cache at runtime (both dynamic, lost when the container is recreated). For the perf tables this isn't worth it: see Performance tests.
- From the devcontainer, use `tsql -H ase -p 5000 -U sa -P Password1` with `TDSVER=5.0`, or `freebcp`. `docker logs dd-ase` and `docker exec` work only from WSL.

## Commands

```shell
poetry install                                   # install with dev dependencies
poetry run unittest-parallel -j 8                # all tests
poetry run python -m unittest -k <test>          # tests matching a name
poetry run python -m unittest -f -k <test>       # same, stop at the first failure
poetry run python -m unittest tests.test_query   # one module
poetry run ruff format .                         # formatter (the only lint step)
poetry run python3 -m data_diff <db1_uri> <table1> <db2_uri> <table2> --verbose
```

- Tests use `unittest`, not pytest.
- `.github/workflows/pr-validate.yml` runs formatting, unit tests on Python 3.9-3.11, and every database pair on 3.11. Pushing a `v*` tag matching the package version runs `release.yml`, which publishes the wheel.
- `CONTRIBUTING.md` still says `black -l 120`. CI uses `ruff format`.
- `LOG_LEVEL=DEBUG` prints every query and the type detected for each column.

## Test configuration

`tests/common.py` holds a `TEST_<DB>_CONN_STRING` for each database. Those with a `None` value are skipped silently, so a database that isn't configured looks like a passing run. Check the `Testing databases:` line printed at import.

Override connection strings in `tests/local_settings.py` (git-ignored), or use the `DATADIFF_*_URI` environment variables. MySQL, PostgreSQL and DuckDB have non-`None` defaults, so MySQL and PostgreSQL must be running.

`TEST_ACROSS_ALL_DBS` controls which pairs `tests/test_database_types.py` runs:

| Value | Pairs |
| --- | --- |
| unset or nonzero integer | every database against PostgreSQL and Snowflake, both directions |
| `0` | PostgreSQL against itself |
| `full` | every configured database against every other |

Db2 against SQL Server is only covered by `TEST_ACROSS_ALL_DBS=full` and `tests/test_cross_db_fact.py`. ASE against each database is in `tests/test_cross_db_types.py`, `tests/test_cross_db_fact.py` and `tests/test_sybase.py`, and CI's `sybase` job runs `-k Sybase -k sybase` with `TEST_ACROSS_ALL_DBS=full`.

`N_SAMPLES` sets the row count per type test (default 50), and `N_THREADS` sets the thread count (default 1).

## Performance tests

`dev/perf/seed.py` downloads Microsoft's `AdventureWorksDW2022.bak` (101 MB, checksum-pinned, git-ignored in `dev/perf/data/`), restores it in the SQL Server container, grows `FactProductInventory` with a cross join, then bulk-loads identical rows into PostgreSQL and Db2. `fact_2x` has 1.55M rows and `fact_13x` has 10.09M.

```shell
poetry run python dev/perf/seed.py                       # about 1 minute
poetry run python dev/perf/run_perf.py fact_2x fact_13x  # about 15 minutes, writes benchmark_<revision>.jsonl
```

`run_perf.py` fails if data-diff doesn't report exactly the rows it changed. Results compare database pairs on one machine. They aren't production numbers, because Db2 Community Edition is limited to 4 cores and 16 GB, and the ASE trial runs in a container.

With `DATADIFF_SYBASE_URI` set, `seed.py` also loads ASE, and `seed.py --only sybase` loads it from the CSVs a previous run exported. ASE has no server-side load, so `freebcp` sends the rows over TDS into `perfdb`, which has its own devices `perfdata` (2,500 MB) and `perflog` (1,000 MB) in `/opt/sap/data`. Measured `sp_spaceused`: `fact_2x` 61 MB, `fact_13x` 399 MB. The primary key is added after the load, and `seed.py` checks counts, sums and the data-diff checksum against PostgreSQL. `run_perf.py` adds ASE in both directions against the other three. `PERF_PAIRS=Sybase:PostgreSQL,...` runs only some pairs, and `SYBASE_CONFIG` labels the results.

The 2 GB defaults are enough. The perf tables (460 MB) already fit in the 768 MB data cache, and a run at `ASE_MEM=4g` with `max memory` at 3.25 GB and a 2 GB data cache was no faster. Timings on this machine vary by about 20% from one run to the next, so repeat a pair before treating a difference of that size as real.

## Database behavior that's easy to get wrong

- **Db2 truncates; PostgreSQL and SQL Server round.** This applies to `CAST`, `DECIMAL` inserts and `TIMESTAMP(n)`, so rounding must be explicit.
- **Db2 `ibm_db_dbi` turns autocommit off.** The driver turns it on. Without that, a `CREATE TABLE` holds catalog locks forever, which only shows up as a hang in parallel runs.
- **A failed Db2 fetch keeps its table locked.** It raises a plain `Exception`, not `ibm_db.Error`, and the transaction stays open despite autocommit, so `_query_cursor` rolls back.
- **Db2 `HASH` rejects `CLOB`.** `VARCHAR(x)` raises an error above 32,672 bytes, while `CAST(x AS VARCHAR(n))` truncates silently, so never use the cast for hashing.
- **Db2 `VARCHAR_FORMAT` renders a number that doesn't fit as `####`.** Those values all hash the same, so `normalize_number` checks the range first.
- **`coltype.rounds` applies to `TIME` too.** PostgreSQL, SQL Server and Db2 truncate when it's false, but Snowflake and BigQuery still round. A truncating side can be the lowest precision at 7 digits, so cap at 6.
- **SQL Server `uniqueidentifier` doesn't sort like a string**, and renders in uppercase. Key ranges compare it through `key_range_expr`, which can't use an index, so these keys are slower.
- **Locale collations hide casing bugs.** PostgreSQL's default sort ignores case, so test string keys on a binary collation too. Db2 and DuckDB use one.
- **Close API connections.** PostgreSQL connections from `connect_to_table` stay in a transaction, which blocks `DROP TABLE`. Call `segment.database.close()` when done.
- **`NVARCHAR(MAX)` is expensive on SQL Server.** It made checksums about 3 times slower, so only text columns use it. `FORMAT()` is slower still, and adds thousands separators.
- **Db2 table and column names are case-sensitive here.** data-diff quotes them, and Db2 stores unquoted names in uppercase, so pass `FACT`, not `fact`, for a table created without quotes.

### SAP ASE behavior

- **The server is cp850, and FreeTDS logs in with an empty character set**, so the connection is always cp850. SQL text must only contain cp850 characters, and the dialect writes others as `U&'\XXXX'`. `set char_convert utf8` changes the server but not FreeTDS, which then corrupts the statement.
- **No server-side UTF-8.** cp850 text goes through `univarchar`, where `str_replace` swaps each non-ASCII character for Private Use Area characters U+E000+b, one per UTF-8 byte. Then every 00 and E0 byte is removed from the UTF-16LE bytes. Swapping bytes directly can't work, because the 128 mappings form a cycle. Unicode text is converted one UTF-16 code unit at a time, which makes about 1.6 KB of SQL per character, so it has a length limit.
- **Converting to `univarchar` stops at the first NUL**, silently. NUL is escaped as 01 02 (and 01 as 01 03) first.
- **Expressions stop at 16,384 bytes without an error**: `+`, `replicate` and `convert` all truncate, and `univarchar` stops at 8,192 units. Every path that could exceed it checks first. `convert(int, 'message ' + <row value>)` raises the error, and it must depend on a row, or ASE may raise it while compiling.
- **`convert(varchar, univarchar)` turns other characters into `?`**, silently, so equal-looking values can come from different text. Never convert Unicode text to cp850 without checking it converts back.
- **ASE cancels unread statements in a batch.** pyodbc returns after the first result, so `_query_cursor` reads every result set, which is also how an error in a later statement surfaces. Without that, 228 of 1,001 inserts ran.
- **`''` is one space, and `varchar` drops trailing spaces.** Never write `''` in generated SQL. `text` keeps trailing spaces, but converting it drops them, so they're added back from `datalength`.
- **Columns are NOT NULL unless they say otherwise.** The dialect writes `NULL` on every non-key column, except `bit`, which can't be NULL.
- **No FULL OUTER JOIN, LIMIT, OFFSET, DROP TABLE IF EXISTS or multi-row VALUES.** ORDER BY isn't allowed in a derived table, and derived-table columns must have names. Brackets quote names, but not `#temp` names. `IF ... CREATE TABLE` compiles the CREATE even when the table exists, so it runs through `EXEC()`.
- **`datetime` and `time` store 1/300 s ticks and show them truncated** (tick 299 is .996667, shown as .996). The dialect recovers the tick from the milliseconds and uses its exact value. Casts of numbers truncate, literals with too many decimals fail, and `money` shows 2 decimals.
- **FreeTDS returns `unsigned int` as signed** and `unitext` as bytes. Key bounds and downloaded rows are fetched as server-side strings, so neither reaches a comparison.
- **The procedure cache fills up** when parallel diffs compile Unicode checksums. `_query_cursor` retries error 701 on a new cursor.
- **A failed fetch doesn't keep locks**, unlike Db2, but the driver still cancels and closes the cursor.

## Architecture

**Entry points.** The CLI is `data_diff/__main__.py`; the Python API is in `data_diff/__init__.py`. Both build a `TableSegment` per table and hand them to a differ. `connect()` in `databases/_connect.py` turns a URI, TOML config or dict into a `Database` through `DATABASE_BY_SCHEME`.

**Two diff algorithms.** The CLI picks joindiff when both tables are in the same database, and hashdiff otherwise:

- `HashDiffer` (`hashdiff_tables.py`) works across databases. It splits the key range into segments, compares `count_and_checksum()` from each side, and recurses into segments that differ. Below `bisection_threshold` it downloads the rows and compares them locally.
- `JoinDiffer` (`joindiff_tables.py`) works within one database, using an outer join. It has `isinstance` branches per database (`MsSQL`, `Oracle`, `MySQL` and others) for SQL that doesn't generalize, so a new database may need its own branch here.

**Cross-database matching.** Hashdiff only works if both databases produce byte-identical strings for equal values. Each column is normalized to a canonical string, the columns are concatenated, and the result is hashed:

```sql
sum(md5_as_int(concat(normalize_*(column), ...)))
```

`render_checksum` in `databases/base.py` builds this. These dialect methods must agree exactly across all databases:

- `normalize_timestamp`, `normalize_number`, `normalize_boolean`, `normalize_uuid`
- `md5_as_int`, which takes the last `CHECKSUM_HEXDIGITS` (12) hex digits of the MD5 as an integer and subtracts `CHECKSUM_OFFSET`. `str_to_checksum` in `tests/common.py` is the reference implementation.

The hash input must also match, so a database whose default string encoding isn't UTF-8 has to convert before hashing. Most false differences between databases come from these methods.

**Query layer.** `data_diff/queries/` is a small SQL AST (`api.py` is the public surface, `ast_classes.py` the nodes). `BaseDialect` in `databases/base.py` compiles it through `render_*` methods, which is where to override syntax a dialect doesn't share.

**Driver shape.** Each file in `data_diff/databases/` has a `Dialect(BaseDialect)` for SQL generation and type mapping (`TYPE_CLASSES`, `ROUNDS_ON_PREC_LOSS`) and a `Database` subclass for connections and schema queries. The two are linked through `DIALECT_CLASS`. Drivers with per-thread cursor connections extend `ThreadedDatabase`. Third-party drivers are imported on demand inside an `@import_helper("<extra>")` function, never at module level.

## Adding a database

`docs/new-database-driver-guide.rst` covers the driver itself, though some of its paths are out of date (for example `data_diff.base`). `databases/mssql.py` is a useful template: it's a threaded driver with a custom `limit_select` and a three-part table path.

A new database touches all of these:

- `data_diff/databases/<name>.py`: the driver
- `data_diff/databases/__init__.py`: export the class
- `data_diff/databases/_connect.py`: import it and add it to `DATABASE_BY_SCHEME` (plus any URI special case in `connect_to_uri`)
- `pyproject.toml`: the optional dependency, `[tool.poetry.extras]`, `all-dbs` and the dev dependencies
- `tests/common.py`: `TEST_<DB>_CONN_STRING` and the `CONN_STRINGS` entry
- `tests/test_database_types.py`: the `DATABASE_TYPES` entry
- `tests/test_database.py`, `tests/test_diff_tables.py` and `tests/test_joindiff.py`: `TEST_DATABASES`
- `docker-compose.yml`, `dev/dev.env`, and `.github/workflows/pr-validate.yml`
