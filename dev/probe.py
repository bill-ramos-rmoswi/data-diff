"""Run one SQL statement on one or more of the configured test databases, with compact output.

Probes go through data-diff's own drivers, so they see the same connection settings, character
sets and cursor handling as the tests (for example ASE's cp850 connection, and Db2's autocommit).

    poetry run python dev/probe.py sybase "select @@version"
    poetry run python dev/probe.py postgres,mssql,db2 "select cast(1.125 as decimal(3,2))"
    poetry run python dev/probe.py all -f probe.sql
    poetry run python dev/probe.py sybase --perf --repeat 3 "select count(*) from fact_2x"

Each database's output is at most --max-rows rows, and an error is one line, so the output
stays short enough to read in a terminal or an agent's context.
"""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ALIASES = {
    "postgres": "POSTGRESQL",
    "postgresql": "POSTGRESQL",
    "pg": "POSTGRESQL",
    "mssql": "MSSQL",
    "sqlserver": "MSSQL",
    "db2": "DB2",
    "mysql": "MYSQL",
    "sybase": "SYBASE",
    "ase": "SYBASE",
}
ALL = ["postgres", "mssql", "db2", "sybase", "mysql"]


def uri_for(name: str, perf: bool) -> str:
    var = ALIASES.get(name.lower())
    if var is None:
        sys.exit(f"Unknown database {name!r}. Use one of: {', '.join(sorted(ALIASES))}, or all")
    if var == "SYBASE" and perf:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "perf"))
        from seed import sybase_perf_uri

        return sybase_perf_uri()
    uri = os.environ.get(f"DATADIFF_{var}_URI")
    if not uri:
        sys.exit(f"DATADIFF_{var}_URI isn't set")
    return uri


def one_line(e: BaseException) -> str:
    text = " ".join(str(e).split())
    return f"{type(e).__name__}: {text[:400]}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("databases", help="Comma-separated names (postgres, mssql, db2, sybase, mysql), or all")
    p.add_argument("sql", nargs="?", help="The statement to run")
    p.add_argument("-f", "--file", help="Read the statement from a file instead")
    p.add_argument("--perf", action="store_true", help="On ASE, use the perf database (perfdb) instead of testdb")
    p.add_argument("--repeat", type=int, default=1, help="Run N times and print the median time")
    p.add_argument("--max-rows", type=int, default=20)
    p.add_argument("--width", type=int, default=200, help="Cut each printed row at this many characters")
    args = p.parse_args()

    if bool(args.sql) == bool(args.file):
        p.error("give either the SQL or -f FILE")
    sql = args.sql or open(args.file).read()
    names = ALL if args.databases == "all" else args.databases.split(",")

    from data_diff import connect

    failed = False
    for name in names:
        label = name.lower()
        if args.databases == "all" and not os.environ.get(f"DATADIFF_{ALIASES[label]}_URI"):
            print(f"[{label}] skipped: not configured")
            continue
        try:
            db = connect(uri_for(name, args.perf))
        except Exception as e:
            print(f"[{label}] connect failed: {one_line(e)}")
            failed = True
            continue
        try:
            times = []
            for _ in range(args.repeat):
                start = time.monotonic()
                rows = db.query(sql, list)
                times.append(time.monotonic() - start)
            timing = f" in {statistics.median(times):.3f}s" + (f" (median of {args.repeat})" if args.repeat > 1 else "")
            rows = rows or []
            print(f"[{label}] {len(rows)} row(s){timing}")
            for row in rows[: args.max_rows]:
                print("  " + repr(tuple(row))[: args.width])
            if len(rows) > args.max_rows:
                print(f"  ... {len(rows) - args.max_rows} more")
        except Exception as e:
            print(f"[{label}] error: {one_line(e)}")
            failed = True
        finally:
            db.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
