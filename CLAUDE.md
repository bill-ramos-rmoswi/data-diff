# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status and current goal

Upstream Datafold stopped developing open source data-diff in May 2024, so this fork (`bill-ramos-rmoswi/data-diff`) owns its CI and releases. Don't open PRs against `datafold/data-diff`; pass `--repo bill-ramos-rmoswi/data-diff` to `gh`.

Goal: use the Python API to validate data between IBM Db2 12.1, SQL Server 2025 (latest CU) and PostgreSQL 18.6, deployed as a wheel attached to a GitHub Release.

Pinned targets are in `docker-compose.yml`: `db2:12.1.5.0`, `mssql/server:2025-CU9-GDR1-ubuntu-24.04` and `postgres:18.6`. MySQL stays in the stack because `test_api.py`, `test_sql.py` and `test_main.py` hardcode it.

## Environment

Work inside the devcontainer, opened from the WSL clone at `~/repos/data-diff`: Python 3.11, Poetry 1.8.5, ODBC Driver 18, `sqlcmd`/`bcp` and `psql` are preinstalled, and `DATADIFF_*_URI` is set to compose service names.

The container has no Docker socket. Start, stop and reset databases from a WSL terminal:

```shell
docker compose up -d --wait postgres mysql mssql db2     # Db2 takes a minute or more when its volume is new
docker compose --profile legacy up -d                    # stale databases: presto, trino, clickhouse, vertica
docker compose down -v                                   # delete all database volumes
```

From outside the compose network, SQL Server is on host port **14330**, because a local instance may hold 1433. Db2 needs `dev/db2/init.sh`, which raises the transaction log limits, and its healthcheck waits for that.

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

Db2 against SQL Server is only covered by `TEST_ACROSS_ALL_DBS=full` and `tests/test_cross_db_fact.py`.

`N_SAMPLES` sets the row count per type test (default 50), and `N_THREADS` sets the thread count (default 1).

## Performance tests

`dev/perf/seed.py` downloads Microsoft's `AdventureWorksDW2022.bak` (101 MB, checksum-pinned, git-ignored in `dev/perf/data/`), restores it in the SQL Server container, grows `FactProductInventory` with a cross join, then bulk-loads identical rows into PostgreSQL and Db2. `fact_2x` has 1.55M rows and `fact_13x` has 10.09M.

```shell
poetry run python dev/perf/seed.py                       # about 1 minute
poetry run python dev/perf/run_perf.py fact_2x fact_13x  # about 15 minutes, writes benchmark_<revision>.jsonl
```

`run_perf.py` fails if data-diff doesn't report exactly the rows it changed. Results compare database pairs on one machine. They aren't production numbers, because Db2 Community Edition is limited to 4 cores and 16 GB.

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
