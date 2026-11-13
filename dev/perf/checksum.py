"""The full data-diff checksum of a perf table on each database, and how long it takes.

Run it before and after a change to a dialect's checksum SQL. Every database must print the
same count and checksum, and so must each run, unless the change is meant to alter values.

    poetry run python dev/perf/checksum.py [table] [databases] [repeat]
    poetry run python dev/perf/checksum.py fact_13x PostgreSQL,Db2,Sybase,MsSQL 3
    SHOW_SQL=1 poetry run python dev/perf/checksum.py fact_2x Db2     # also print the SQL
    poetry run python dev/perf/checksum.py oltp_order_line PostgreSQL,MsSQL --shape oltp

table is fact_2x (default) or fact_13x, from dev/perf/seed.py, or oltp_order_line with the oltp
shape (run_perf.SHAPES; the default for oltp_* tables). databases is a comma-separated
list of PostgreSQL, Db2, Sybase and MsSQL, by default PostgreSQL,Db2,Sybase. repeat (default 1)
runs each checksum that many times and prints the median and the spread. It exits with 1 if
the databases give different results.

This times the checksum query alone. A full diff also bisects and downloads rows, so a faster
checksum gives a smaller gain end to end: dev/perf/profile_diff.py shows the split.
"""

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from seed import sybase_perf_uri

from data_diff import connect_to_table
from data_diff.queries.api import Count, this
from data_diff.queries.extras import Checksum, NormalizeAsString

KEY_COLUMNS = ("ScaleKey", "ProductKey", "DateKey")
EXTRA_COLUMNS = ("MovementDate", "UnitCost", "UnitsIn", "UnitsOut", "UnitsBalance")
# The same as run_perf.SHAPES, which this doesn't import because run_perf needs every DATADIFF_*_URI
SHAPES = {
    "fact": (KEY_COLUMNS, EXTRA_COLUMNS),
    "oltp": (
        ("OrderId", "LineNumber"),
        (
            "ProductCode", "ProductName", "CustomerName", "Comment", "TrackingNumber", "Quantity", "UnitPrice",
            "Discount", "LineTotal", "OrderDate", "ShipDate", "ModifiedDate",
        ),
    ),
}  # fmt: skip


def uri(name: str) -> str:
    if name == "Sybase":
        # The perf tables are in their own ASE database
        return sybase_perf_uri()
    return os.environ[f"DATADIFF_{name.upper()}_URI"]


def time_checksum(name: str, table: str, repeat: int = 1, show_sql: bool = False, shape: str = "fact"):
    """Returns (count, checksum, list of seconds, length of the SQL) for one database."""
    key, extra = SHAPES[shape]
    segment = connect_to_table(uri(name), table, key, extra_columns=extra).with_schema()
    try:
        columns = key + extra
        query = segment.make_select().select(Count(), Checksum([NormalizeAsString(this[c]) for c in columns]))
        sql = segment.database.compile(query)
        if show_sql:
            print(sql)
        seconds = []
        for _ in range(repeat):
            start = time.monotonic()
            count, checksum = segment.database.query(query, tuple)
            seconds.append(time.monotonic() - start)
        return count, checksum, seconds, len(sql)
    finally:
        segment.database.close()


def spread(seconds: list) -> float:
    """(max - min) / median, the run-to-run noise of one measurement."""
    return (max(seconds) - min(seconds)) / statistics.median(seconds) if len(seconds) > 1 else 0.0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("table", nargs="?", default="fact_2x")
    p.add_argument("databases", nargs="?", default="PostgreSQL,Db2,Sybase")
    p.add_argument("repeat", nargs="?", type=int, default=1)
    p.add_argument("--shape", choices=list(SHAPES), help="default: oltp for oltp_* tables, else fact")
    args = p.parse_args()
    table, names, repeat = args.table, args.databases.split(","), args.repeat
    shape = args.shape or ("oltp" if table.startswith("oltp_") else "fact")
    results = set()
    for name in names:
        count, checksum, seconds, sql_length = time_checksum(
            name, table, repeat, bool(os.environ.get("SHOW_SQL")), shape
        )
        results.add((count, checksum))
        timing = f"{statistics.median(seconds):.2f}s"
        if repeat > 1:
            timing += f" (median of {repeat}, spread {spread(seconds):.0%})"
        print(f"{name:10} {table} count={count} checksum={checksum} {timing} sql={sql_length} chars", flush=True)
    if len(results) > 1:
        print("The databases give different results")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
