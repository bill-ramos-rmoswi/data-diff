# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status and current goal

Upstream Datafold stopped developing open source data-diff in May 2024, so upstream won't review or merge changes here.

Current goal: add IBM Db2 12.x support and test it against SQL Server 2025 (latest CU) and PostgreSQL 18.6 (community release).

Where the repo stands against that goal:

- There is no Db2 code yet: no driver, extra, compose service or test wiring.
- `docker-compose.yml` has no SQL Server service, and its Postgres is pinned to `postgres:14.1-alpine`. The `postgresql-data` volume holds Postgres 14 data, which Postgres 18 can't open, so recreate the volume after bumping the image.
- `TEST_MSSQL_CONN_STRING` comes from `DATADIFF_MSSQL_URI`, and neither CI workflow sets it, so CI doesn't run the MsSQL driver.
- `pyodbc` is an optional extra (`mssql`) but not a dev dependency. `MsSQL` hardcodes `ODBC Driver 18 for SQL Server`, which must be installed on the host running the tests.

## Commands

```shell
poetry install                                   # install with dev dependencies
docker compose up -d mysql postgres              # databases for tests; MySQL is the minimum (see below)
poetry run unittest-parallel -j 16               # all tests
poetry run python -m unittest -k <test>          # tests matching a name
poetry run python -m unittest -f -k <test>       # same, stop at the first failure
poetry run python -m unittest tests.test_query   # one module
poetry run ruff format .                         # formatter (the only lint step)
poetry run python3 -m data_diff <db1_uri> <table1> <db2_uri> <table2> --verbose
```

- Tests use `unittest`, not pytest.
- CI runs `TEST_ACROSS_ALL_DBS=0 poetry run unittest-parallel -j 16` on Python 3.8-3.11, and the full cross-database suite on 3.11.
- CI's formatter check is `ruff format`. `CONTRIBUTING.md` still says `black -l 120`; line length is 120 either way.
- `LOG_LEVEL=DEBUG` prints every query and the type detected for each column.

## Test configuration

`tests/common.py` holds a `TEST_<DB>_CONN_STRING` for each database. Those with a `None` value are skipped silently, so a database that isn't configured looks like a passing run. Check the `Testing databases:` line printed at import.

Override connection strings in `tests/local_settings.py` (git-ignored), or use the `DATADIFF_*_URI` environment variables. MySQL, PostgreSQL and DuckDB have non-`None` defaults, so MySQL and PostgreSQL must be running, or be set to `None` in `local_settings.py`.

`TEST_ACROSS_ALL_DBS` controls which pairs `tests/test_database_types.py` runs:

| Value | Pairs |
| --- | --- |
| unset or nonzero integer | every database against PostgreSQL and Snowflake, both directions |
| `0` | PostgreSQL against itself |
| `full` | every configured database against every other |

Db2 against SQL Server is only covered by `TEST_ACROSS_ALL_DBS=full`. To test just Db2, SQL Server and PostgreSQL, set the other connection strings to `None`.

`N_SAMPLES` sets the row count per type test (default 50), and `N_THREADS` sets the thread count (default 1).

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
- `tests/test_database.py`: `TEST_DATABASES`
- `docker-compose.yml`, `dev/dev.env`, and `.github/workflows/ci.yml` and `ci_full.yml`
