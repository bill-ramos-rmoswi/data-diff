"""The full data-diff checksum of a perf table on each database, and how long it takes.

Run it before and after a change to a dialect's checksum SQL. Every database must print the
same count and checksum, and so must each run, unless the change is meant to alter values.

    poetry run python dev/perf/checksum.py [table] [databases]
    poetry run python dev/perf/checksum.py fact_13x PostgreSQL,Db2,Sybase,MsSQL
    SHOW_SQL=1 poetry run python dev/perf/checksum.py fact_2x Db2     # also print the SQL

table is fact_2x (default) or fact_13x, from dev/perf/seed.py. databases is a comma-separated
list of PostgreSQL, Db2, Sybase and MsSQL, by default PostgreSQL,Db2,Sybase. It exits with 1
if the databases give different results.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from seed import sybase_perf_uri

from data_diff import connect_to_table
from data_diff.queries.api import Count, this
from data_diff.queries.extras import Checksum, NormalizeAsString

KEY_COLUMNS = ("ScaleKey", "ProductKey", "DateKey")
EXTRA_COLUMNS = ("MovementDate", "UnitCost", "UnitsIn", "UnitsOut", "UnitsBalance")


def uri(name: str) -> str:
    if name == "Sybase":
        # The perf tables are in their own ASE database
        return sybase_perf_uri()
    return os.environ[f"DATADIFF_{name.upper()}_URI"]


def main() -> int:
    table = sys.argv[1] if len(sys.argv) > 1 else "fact_2x"
    names = sys.argv[2].split(",") if len(sys.argv) > 2 else ["PostgreSQL", "Db2", "Sybase"]
    results = set()
    for name in names:
        segment = connect_to_table(uri(name), table, KEY_COLUMNS, extra_columns=EXTRA_COLUMNS).with_schema()
        columns = KEY_COLUMNS + EXTRA_COLUMNS
        query = segment.make_select().select(Count(), Checksum([NormalizeAsString(this[c]) for c in columns]))
        sql = segment.database.compile(query)
        if os.environ.get("SHOW_SQL"):
            print(sql)
        start = time.monotonic()
        count, checksum = segment.database.query(query, tuple)
        seconds = time.monotonic() - start
        segment.database.close()
        results.add((count, checksum))
        print(f"{name:10} {table} count={count} checksum={checksum} {seconds:.2f}s sql={len(sql)} chars")
    if len(results) > 1:
        print("The databases give different results")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
