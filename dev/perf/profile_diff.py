"""Where the time of one full diff goes: checksum queries, key ranges, row downloads, or the client.

    poetry run python dev/perf/profile_diff.py Db2 PostgreSQL fact_13x
    poetry run python dev/perf/profile_diff.py MsSQL PostgreSQL fact_2x --scenario updated
    PERF_ALGORITHM=rowhash poetry run python dev/perf/profile_diff.py Sybase PostgreSQL fact_2x

Database names are those of dev/perf/run_perf.py (PostgreSQL, MsSQL, Db2, Sybase). The updated
scenario changes the same rows as run_perf.py in the target, and puts them back afterwards.

It times each TableSegment call that queries a database. Both sides and all threads run at
once, so the summed seconds can exceed the wall time. "Wall %" is the share of the diff's
wall time during which at least one query of that kind was running, and "client only" is the
time no query was running at all (Python, comparing rows, thread hand-offs).

Use it before optimizing a step: if checksums cover 40% of the wall time, making them 10%
faster saves at most 4% end to end, which this machine's noise hides.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

import run_perf  # noqa: E402  reads DATADIFF_*_URI, like run_perf.py itself
from data_diff.queries.api import commit  # noqa: E402
from data_diff.table_segment import TableSegment  # noqa: E402

CATEGORIES = {
    "count_and_checksum": "checksum",
    "count": "count",
    "query_key_range": "key range",
    "get_values": "download rows",
    "get_key_hashes": "key hashes",
    "get_rows_by_keys": "download by key",
    "with_schema": "schema",
}
_calls = []
_lock = threading.Lock()


def _wrap(method_name: str, category: str):
    original = getattr(TableSegment, method_name)

    def timed(self, *args, **kwargs):
        start = time.monotonic()
        try:
            return original(self, *args, **kwargs)
        finally:
            with _lock:
                _calls.append((category, self.database.name, start, time.monotonic()))

    setattr(TableSegment, method_name, timed)


def covered(intervals) -> float:
    """Seconds covered by the union of (start, end) intervals."""
    total, end_so_far = 0.0, None
    for start, end in sorted(intervals):
        if end_so_far is None or start > end_so_far:
            total += end - start
            end_so_far = end
        elif end > end_so_far:
            total += end - end_so_far
            end_so_far = end
    return total


def report(calls, wall: float) -> dict:
    rows = []
    for category in sorted({c for c, *_ in calls}):
        mine = [(s, e) for c, _, s, e in calls if c == category]
        rows.append(
            {
                "category": category,
                "calls": len(mine),
                "summed_sec": round(sum(e - s for s, e in mine), 3),
                "wall_sec": round(covered(mine), 3),
                "wall_pct": round(100 * covered(mine) / wall, 1),
            }
        )
    in_queries = covered([(s, e) for _, _, s, e in calls])
    return {"wall_sec": round(wall, 3), "client_only_sec": round(wall - in_queries, 3), "categories": rows}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("table", nargs="?", default="fact_2x")
    p.add_argument("--scenario", choices=["identical", "updated"], default="identical")
    p.add_argument("--json", help="Also write the result to this file")
    args = p.parse_args()

    for method_name, category in CATEGORIES.items():
        if hasattr(TableSegment, method_name):
            _wrap(method_name, category)

    conn = run_perf.connect(run_perf.URIS[args.target], shared=False)
    table_sql = conn.dialect.quote(args.table)
    where = run_perf.changed_rows(conn, table_sql).split(" WHERE ")[1]
    cost = conn.dialect.quote("UnitCost")
    if args.scenario == "updated":
        conn.query(f"UPDATE {table_sql} SET {cost} = {cost} + 1 WHERE {where}", None)
        conn.query(commit)
    try:
        start = time.monotonic()
        diff, _ = run_perf.run_diff(args.source, args.target, args.table)
        wall = time.monotonic() - start
    finally:
        if args.scenario == "updated":
            conn.query(f"UPDATE {table_sql} SET {cost} = {cost} - 1 WHERE {where}", None)
            conn.query(commit)
        conn.close()

    result = report(_calls, wall)
    result.update(
        source=args.source,
        target=args.target,
        table=args.table,
        scenario=args.scenario,
        algorithm=run_perf.ALGORITHM,
        threads=run_perf.THREADS,
        diff_lines=len(diff),
    )
    print(
        f"{args.source} -> {args.target} {args.table} {args.scenario} {run_perf.ALGORITHM}: "
        f"{wall:.1f}s wall, {len(diff)} diff lines, {run_perf.THREADS} threads"
    )
    print(f"  {'step':<16} {'calls':>6} {'summed s':>9} {'wall s':>8} {'wall %':>7}")
    for row in result["categories"]:
        print(
            f"  {row['category']:<16} {row['calls']:>6} {row['summed_sec']:>9.1f} {row['wall_sec']:>8.1f} "
            f"{row['wall_pct']:>6.1f}%"
        )
    print(
        f"  {'client only':<16} {'':>6} {'':>9} {result['client_only_sec']:>8.1f} "
        f"{100 * result['client_only_sec'] / wall:>6.1f}%"
    )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as file:
            json.dump(result, file, indent=2)


if __name__ == "__main__":
    main()
