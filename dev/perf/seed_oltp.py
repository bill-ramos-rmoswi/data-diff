"""Seeds oltp_order_line (2,000,000 rows) into SQL Server, PostgreSQL, Db2 and ASE (perfdb), then checks them.

    poetry run python dev/perf/seed_oltp.py            # about 3-5 minutes
    poetry run python dev/perf/seed_oltp.py --check    # only the check

Shape: an order line, wide rows (about 330 bytes of text), composite key (OrderId, LineNumber) with
500,000 orders of 4 lines, varchar columns, NULLs in 4 columns, a timestamp (whole seconds, so ASE
datetime stores it exactly), dates and decimals. ASCII text without commas or quotes, so the CSV is
safe for all four loaders. It reuses the helpers of dev/perf/seed.py, the same way.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed  # noqa: E402  reads DATADIFF_*_URI
from data_diff import connect, connect_to_table  # noqa: E402
from data_diff.queries.api import commit  # noqa: E402

NAME = "oltp_order_line"
ROWS = 2_000_000
KEY = ["OrderId", "LineNumber"]
EXTRA = [
    "ProductCode", "ProductName", "CustomerName", "Comment", "TrackingNumber", "Quantity", "UnitPrice",
    "Discount", "LineTotal", "OrderDate", "ShipDate", "ModifiedDate",
]  # fmt: skip
CSV = seed.DATA_DIR / f"{NAME}.csv"

# (name, type, nullable)
COLUMNS = [
    ("OrderId", "INT", False), ("LineNumber", "INT", False),
    ("ProductCode", "VARCHAR(20)", False), ("ProductName", "VARCHAR(100)", False),
    ("CustomerName", "VARCHAR(60)", False), ("Comment", "VARCHAR(255)", True),
    ("TrackingNumber", "VARCHAR(25)", True), ("Quantity", "INT", False),
    ("UnitPrice", "NUMERIC(19, 4)", False), ("Discount", "NUMERIC(5, 4)", True),
    ("LineTotal", "NUMERIC(19, 4)", False), ("OrderDate", "TS", False),
    ("ShipDate", "DATE", True), ("ModifiedDate", "TS", False),
]  # fmt: skip
TS = {"mssql": "DATETIME2(0)", "postgres": "TIMESTAMP(0)", "db2": "TIMESTAMP(0)", "sybase": "DATETIME"}


def ddl(kind: str, quote) -> str:
    cols = []
    for name, type_, nullable in COLUMNS:
        type_ = TS[kind] if type_ == "TS" else type_
        cols.append(f"{quote(name)} {type_} {'NULL' if nullable else 'NOT NULL'}")
    return ", ".join(cols)


def build_in_mssql():
    start = time.monotonic()
    conn = connect(seed.MSSQL_URI, shared=False)
    conn.query(f"DROP TABLE IF EXISTS {NAME}", None)
    # n runs 0..ROWS-1. Everything is a function of n, so the table is the same every time.
    conn.query(
        f"""SELECT OrderId, LineNumber, ProductCode, ProductName, CustomerName,
        CASE WHEN n % 10 < 3 THEN NULL ELSE 'Note ' + CAST(n * 7919 % 1000003 AS VARCHAR(10)) + ' '
             + REPLICATE('abc', n % 60) + 'z' END AS Comment,
        CASE WHEN n % 5 = 0 THEN NULL ELSE 'TRK' + RIGHT('0000000000' + CAST(n * 104729 % 999999937 AS VARCHAR(10)), 10) END AS TrackingNumber,
        Quantity, UnitPrice, Discount, CAST(Quantity * UnitPrice AS NUMERIC(19, 4)) AS LineTotal,
        OrderDate, ShipDate, DATEADD(SECOND, n % 86400, OrderDate) AS ModifiedDate
        INTO {NAME} FROM (
          SELECT n, n / 4 + 1 AS OrderId, n % 4 + 1 AS LineNumber,
            'PRD-' + RIGHT('000000' + CAST(n * 31 % 99991 AS VARCHAR(10)), 6) AS ProductCode,
            'Product ' + CAST(n % 4999 AS VARCHAR(10)) + ' deluxe edition ' + REPLICATE('widget ', n % 9 + 1) + 'item' AS ProductName,
            'Customer ' + CAST(n / 4 % 20011 AS VARCHAR(10)) + ' Holdings and Sons ' + REPLICATE('x', n / 4 % 17 + 1) AS CustomerName,
            n % 12 + 1 AS Quantity,
            CAST((n * 17 % 100000) / 100.0 + 1 AS NUMERIC(19, 4)) AS UnitPrice,
            CASE WHEN n % 7 = 0 THEN NULL ELSE CAST((n % 25) / 100.0 AS NUMERIC(5, 4)) END AS Discount,
            CAST(DATEADD(SECOND, n * 37 % 129600000, '2020-01-01') AS DATETIME2(0)) AS OrderDate,
            CASE WHEN n % 9 = 0 THEN NULL ELSE CAST(DATEADD(DAY, n % 14 + 1, '2020-01-01') AS DATE) END AS ShipDate
          FROM (SELECT TOP ({ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
                FROM sys.all_objects a CROSS JOIN sys.all_objects b) t
        ) s""",
        None,
    )
    conn.query(f"ALTER TABLE {NAME} ALTER COLUMN OrderId INT NOT NULL", None)
    conn.query(f"ALTER TABLE {NAME} ALTER COLUMN LineNumber INT NOT NULL", None)
    conn.query(f"ALTER TABLE {NAME} ADD PRIMARY KEY (OrderId, LineNumber)", None)
    conn.query(commit)
    conn.close()
    print(f"Built in SQL Server: {time.monotonic() - start:.1f}s", flush=True)


def export_from_mssql():
    start = time.monotonic()
    query = (
        "SELECT OrderId, LineNumber, ProductCode, ProductName, CustomerName, Comment, TrackingNumber, Quantity, "
        "UnitPrice, Discount, LineTotal, CONVERT(char(19), OrderDate, 120), CONVERT(char(10), ShipDate, 23), "
        f"CONVERT(char(19), ModifiedDate, 120) FROM {NAME}"
    )
    seed.bcp(query, "queryout", str(CSV), "-r", "\\n")
    print(f"Exported: {time.monotonic() - start:.1f}s", flush=True)


def load_postgres():
    start = time.monotonic()
    conn = connect(seed.POSTGRESQL_URI, shared=False)
    conn.query(f"DROP TABLE IF EXISTS {NAME}", None)
    conn.query(f"CREATE TABLE {NAME} ({ddl('postgres', conn.dialect.quote)})", None)
    conn.query(commit)
    conn.close()
    uri = urlsplit(seed.POSTGRESQL_URI)
    env = {**os.environ, "PGPASSWORD": uri.password}
    login = ["-h", uri.hostname, "-U", uri.username, "-d", uri.path.lstrip("/")]
    for command in (
        f"\\copy {NAME} FROM '{CSV}' WITH (FORMAT csv)",
        f'ALTER TABLE {NAME} ADD PRIMARY KEY ("OrderId", "LineNumber")',
        f"ANALYZE {NAME}",
    ):
        subprocess.run(["psql", *login, "-q", "-c", command], check=True, env=env)
    print(f"Loaded PostgreSQL: {time.monotonic() - start:.1f}s", flush=True)


def load_db2():
    start = time.monotonic()
    conn = connect(seed.DB2_URI, shared=False)
    table = f'"{conn.default_schema}"."{NAME}"'
    conn.query(f"DROP TABLE IF EXISTS {table}", None)
    conn.query(f'CREATE TABLE {table} ({ddl("db2", conn.dialect.quote)}, PRIMARY KEY ("OrderId", "LineNumber"))', None)
    load = (
        f'LOAD FROM {seed.SERVER_DATA_DIR}/{NAME}.csv OF DEL MODIFIED BY TIMESTAMPFORMAT="YYYY-MM-DD HH:MM:SS" '
        f"REPLACE INTO {table} NONRECOVERABLE"
    )
    conn.query(f"CALL SYSPROC.ADMIN_CMD('{load}')", None)
    conn.query(f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {table} WITH DISTRIBUTION AND INDEXES ALL')", None)
    conn.close()
    print(f"Loaded Db2: {time.monotonic() - start:.1f}s", flush=True)


# freebcp can't load a row of more than about 255 characters into ASE: ASE answers error 4853 "badly formatted
# rows" and loads nothing, while freebcp still says "N rows copied". So the CSV is cut into column groups that
# each stay below that, bulk-loaded into staging tables, and joined into the real table on the server.
SYBASE_PARTS = {"A": range(0, 5), "B": [0, 1, 5], "C": [0, 1, *range(6, 14)]}  # 0-based CSV fields


def load_sybase():
    start = time.monotonic()
    seed.create_sybase_database()
    uri = urlsplit(seed.sybase_perf_uri())
    conn = connect(uri.geturl(), shared=False)
    env = {**os.environ, "TDSVER": "5.0", "TDSPORT": str(uri.port or 5000)}
    ase_type = {name: (TS["sybase"] if type_ == "TS" else type_) + (" NULL" if nullable else " NOT NULL")
                for name, type_, nullable in COLUMNS}  # fmt: skip
    for table in (NAME, *(f"{NAME}_{part}" for part in SYBASE_PARTS)):
        conn.query(f"IF object_id('{table}') IS NOT NULL DROP TABLE {table}", None)
    for part, fields in SYBASE_PARTS.items():
        path = seed.DATA_DIR / f"{NAME}_{part}.csv"
        fields = list(fields)
        subprocess.run(["cut", "-d,", "-f" + ",".join(str(i + 1) for i in fields), str(CSV)],
                       check=True, stdout=path.open("w"))  # fmt: skip
        longest = max(len(line) for line in path.open())
        assert longest < 250, f"part {part}: a row of {longest} characters is too long for freebcp into ASE"
        staging = f"{NAME}_{part}"
        cols = ", ".join(f"{COLUMNS[i][0]} {ase_type[COLUMNS[i][0]]}" for i in fields)
        conn.query(f"CREATE TABLE {staging} ({cols})", None)
        subprocess.run(
            ["freebcp", f"{uri.path.split('/')[1]}.dbo.{staging}", "in", str(path), "-S", uri.hostname, "-U",
             uri.username, "-P", uri.password, "-c", "-t", ",", "-r", "\\n", "-b", "50000"],
            check=True, env=env, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        conn.query(f"CREATE UNIQUE INDEX ix_{staging} ON {staging} (OrderId, LineNumber)", None)
        count = conn.query(f"SELECT count(*) FROM {staging}", int)
        assert count == ROWS, f"{staging}: {count} rows loaded, expected {ROWS}"
    conn.query(f"CREATE TABLE {NAME} ({ddl('sybase', lambda c: c)})", None)
    names = [c[0] for c in COLUMNS]
    where_of = {"A": "a", "B": "b", "C": "c"}
    select = ", ".join(
        f"{where_of[next(p for p, f in SYBASE_PARTS.items() if i in f)]}.{n}" for i, n in enumerate(names)
    )
    conn.query(
        f"INSERT INTO {NAME} SELECT {select} FROM {NAME}_A a JOIN {NAME}_B b ON a.OrderId = b.OrderId "
        f"AND a.LineNumber = b.LineNumber JOIN {NAME}_C c ON a.OrderId = c.OrderId AND a.LineNumber = c.LineNumber",
        None,
    )
    for part in SYBASE_PARTS:
        conn.query(f"DROP TABLE {NAME}_{part}", None)
    conn.query(f"ALTER TABLE {NAME} ADD PRIMARY KEY (OrderId, LineNumber)", None)
    conn.query(f"UPDATE STATISTICS {NAME}", None)
    print(f"  sp_spaceused: {seed.sybase_spaceused(conn, NAME)}", flush=True)
    conn.close()
    print(f"Loaded ASE: {time.monotonic() - start:.1f}s", flush=True)


def check():
    """Row counts, NULL counts, sums and the data-diff checksum must equal PostgreSQL's in every database."""
    uris = {"PostgreSQL": seed.POSTGRESQL_URI, "MsSQL": seed.MSSQL_URI, "Db2": seed.DB2_URI}
    if seed.SYBASE_URI:
        uris["Sybase"] = seed.sybase_perf_uri()
    results = {}
    for label, uri in uris.items():
        conn = connect(uri, shared=False)
        q = conn.dialect.quote
        wide = (lambda c, d=4: f"convert(numeric(38, {d}), {c})") if label == "Sybase" else (lambda c, d=4: c)
        sql = (
            f"SELECT count(*), count({q('Comment')}), count({q('TrackingNumber')}), count({q('Discount')}), "
            f"count({q('ShipDate')}), sum({wide(q('Quantity'), 0)}), sum({wide(q('UnitPrice'))}), sum({wide(q('LineTotal'))}) "
            f"FROM {q(NAME)}"
        )
        aggregates = [str(v) for v in conn.query(sql, list)[0]]
        conn.close()
        segment = connect_to_table(uri, NAME, KEY, extra_columns=EXTRA, thread_count=4)
        checksum = segment.with_schema().count_and_checksum()
        segment.database.close()
        results[label] = (aggregates, checksum)
        print(f"  {label}: {results[label]}", flush=True)
    reference = results["PostgreSQL"]
    bad = [label for label, value in results.items() if value != reference]
    assert not bad, f"{NAME} differs from PostgreSQL in {bad}"
    assert int(reference[0][0]) == ROWS, f"expected {ROWS} rows"
    print("Check passed: identical in every database", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        build_in_mssql()
        export_from_mssql()
        load_postgres()
        load_db2()
        if seed.SYBASE_URI:
            load_sybase()
    check()


if __name__ == "__main__":
    main()
