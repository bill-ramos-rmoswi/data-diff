"""Grows FactProductInventory with a cross join, and loads the result into all three databases.

Run this in the devcontainer, after exporting the base table to dev/perf/data/fact_base.csv:

    bcp "SELECT ProductKey, DateKey, CONVERT(char(10), MovementDate, 23), UnitCost, UnitsIn, UnitsOut,
         UnitsBalance FROM AdventureWorksDW2022.dbo.FactProductInventory ORDER BY ProductKey, DateKey"
        queryout fact_base.csv -S localhost -T -u -c -t "," -r "\\n"

    poetry run python dev/perf/seed.py

SQL Server builds the larger tables. Each one is exported once and bulk-loaded into the other
databases, so every database holds identical rows.
"""
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from data_diff import connect
from data_diff.queries.api import commit

DATA_DIR = Path(__file__).parent / "data"
# Where the Db2 container sees DATA_DIR, because LOAD reads files on the server
DB2_DATA_DIR = "/perf-data"
TABLES = {"fact_2x": 2, "fact_13x": 13}

MSSQL_URI = os.environ["DATADIFF_MSSQL_URI"]
POSTGRESQL_URI = os.environ["DATADIFF_POSTGRESQL_URI"]
DB2_URI = os.environ["DATADIFF_DB2_URI"]


def timed(label: str):
    def decorator(step):
        def run(*args):
            start = time.monotonic()
            step(*args)
            print(f"{label} {' '.join(map(str, args))}: {time.monotonic() - start:.1f}s", flush=True)

        return run

    return decorator


def bcp(*args):
    uri = urlsplit(MSSQL_URI)
    database = uri.path.split("/")[1]
    login = ["-S", f"{uri.hostname},{uri.port or 1433}", "-U", uri.username, "-P", uri.password, "-u", "-d", database]
    subprocess.run(["bcp", *args, *login, "-c", "-t", ","], check=True, stdout=subprocess.DEVNULL)


@timed("Loaded base table into SQL Server")
def load_base_table():
    conn = connect(MSSQL_URI, shared=False)
    conn.query("DROP TABLE IF EXISTS fact_base", None)
    conn.query(
        "CREATE TABLE fact_base (ProductKey INT NOT NULL, DateKey INT NOT NULL, MovementDate DATE NOT NULL, "
        "UnitCost MONEY NOT NULL, UnitsIn INT NOT NULL, UnitsOut INT NOT NULL, UnitsBalance INT NOT NULL)",
        None,
    )
    conn.query(commit)
    conn.close()
    # bcp on Windows ends lines with CRLF
    bcp("fact_base", "in", str(DATA_DIR / "fact_base.csv"), "-r", "\\r\\n")


@timed("Built in SQL Server")
def build_in_mssql(name: str, scale: int):
    tally = ", ".join(f"({i})" for i in range(scale))
    conn = connect(MSSQL_URI, shared=False)
    conn.query(f"DROP TABLE IF EXISTS {name}", None)
    conn.query(
        "SELECT ISNULL(tally.ScaleKey, 0) AS ScaleKey, ProductKey, DateKey, "
        "DATEADD(DAY, tally.ScaleKey, MovementDate) AS MovementDate, "
        "CAST(UnitCost + tally.ScaleKey * 0.01 AS MONEY) AS UnitCost, UnitsIn, UnitsOut, UnitsBalance "
        f"INTO {name} FROM fact_base CROSS JOIN (VALUES {tally}) AS tally(ScaleKey)",
        None,
    )
    conn.query(f"ALTER TABLE {name} ADD PRIMARY KEY (ScaleKey, ProductKey, DateKey)", None)
    conn.query(commit)
    conn.close()


@timed("Exported from SQL Server")
def export_from_mssql(name: str):
    query = (
        "SELECT ScaleKey, ProductKey, DateKey, CONVERT(char(10), MovementDate, 23), UnitCost, UnitsIn, UnitsOut, "
        f"UnitsBalance FROM {name}"
    )
    bcp(query, "queryout", str(DATA_DIR / f"{name}.csv"), "-r", "\\n")


@timed("Loaded into PostgreSQL")
def load_into_postgresql(name: str):
    conn = connect(POSTGRESQL_URI, shared=False)
    conn.query(f"DROP TABLE IF EXISTS {name}", None)
    conn.query(
        f'CREATE TABLE {name} ("ScaleKey" INT NOT NULL, "ProductKey" INT NOT NULL, "DateKey" INT NOT NULL, '
        '"MovementDate" DATE NOT NULL, "UnitCost" NUMERIC(19, 4) NOT NULL, "UnitsIn" INT NOT NULL, '
        '"UnitsOut" INT NOT NULL, "UnitsBalance" INT NOT NULL)',
        None,
    )
    conn.query(commit)
    conn.close()

    uri = urlsplit(POSTGRESQL_URI)
    env = {**os.environ, "PGPASSWORD": uri.password}
    login = ["-h", uri.hostname, "-U", uri.username, "-d", uri.path.lstrip("/")]
    commands = [
        f"\\copy {name} FROM '{DATA_DIR / f'{name}.csv'}' WITH (FORMAT csv)",
        f'ALTER TABLE {name} ADD PRIMARY KEY ("ScaleKey", "ProductKey", "DateKey")',
        f"ANALYZE {name}",
    ]
    for command in commands:
        subprocess.run(["psql", *login, "-q", "-c", command], check=True, env=env)


@timed("Loaded into Db2")
def load_into_db2(name: str):
    conn = connect(DB2_URI, shared=False)
    schema = conn.default_schema
    table = f'"{schema}"."{name}"'
    conn.query(f"DROP TABLE IF EXISTS {table}", None)
    conn.query(
        f'CREATE TABLE {table} ("ScaleKey" INT NOT NULL, "ProductKey" INT NOT NULL, "DateKey" INT NOT NULL, '
        '"MovementDate" DATE NOT NULL, "UnitCost" DECIMAL(19, 4) NOT NULL, "UnitsIn" INT NOT NULL, '
        '"UnitsOut" INT NOT NULL, "UnitsBalance" INT NOT NULL, '
        'PRIMARY KEY ("ScaleKey", "ProductKey", "DateKey"))',
        None,
    )
    load = f"LOAD FROM {DB2_DATA_DIR}/{name}.csv OF DEL REPLACE INTO {table} NONRECOVERABLE"
    conn.query(f"CALL SYSPROC.ADMIN_CMD('{load}')", None)
    runstats = f"RUNSTATS ON TABLE {table} WITH DISTRIBUTION AND INDEXES ALL"
    conn.query(f"CALL SYSPROC.ADMIN_CMD('{runstats}')", None)
    conn.close()


def main():
    assert not dict(parse_qsl(urlsplit(MSSQL_URI).query)).get("database"), "Unexpected database in the URI query"
    load_base_table()
    for name, scale in TABLES.items():
        build_in_mssql(name, scale)
        export_from_mssql(name)
        load_into_postgresql(name)
        load_into_db2(name)


if __name__ == "__main__":
    main()
