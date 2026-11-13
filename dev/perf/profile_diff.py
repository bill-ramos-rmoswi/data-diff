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
from data_diff import connect_to_table, diff_tables  # noqa: E402
from data_diff.hashdiff_tables import DEFAULT_BISECTION_FACTOR, DEFAULT_BISECTION_THRESHOLD  # noqa: E402
from data_diff.queries.api import commit  # noqa: E402
from data_diff.table_segment import TableSegment  # noqa: E402

# Table shapes. fact_* is run_perf.py's. oltp_order_line is seeded by the tuning run's seed_oltp.py.
# "where" is the predicate of the rows the updated scenario changes (about 0.016% of the table), which
# never needs a dialect-specific operator.
SHAPES = {
    "fact": dict(
        key=run_perf.KEY_COLUMNS,
        extra=run_perf.EXTRA_COLUMNS,
        update="UnitCost",
        where=lambda q: f"{q('DateKey')} = 20130615 AND {q('ProductKey')} IN ({run_perf.PRODUCT_KEYS})",
    ),
    "oltp": dict(
        key=("OrderId", "LineNumber"),
        extra=(
            "ProductCode", "ProductName", "CustomerName", "Comment", "TrackingNumber", "Quantity", "UnitPrice",
            "Discount", "LineTotal", "OrderDate", "ShipDate", "ModifiedDate",
        ),
        update="Quantity",
        where=lambda q: f"{q('LineNumber')} = 1 AND {q('OrderId')} IN ({', '.join(str(i) for i in range(3000, 500000, 3000))})",
    ),
}  # fmt: skip

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
    p.add_argument("--shape", choices=list(SHAPES), help="default: oltp for oltp_* tables, else fact")
    p.add_argument("--bisection-factor", type=int, default=DEFAULT_BISECTION_FACTOR)
    p.add_argument("--bisection-threshold", type=int, default=DEFAULT_BISECTION_THRESHOLD)
    p.add_argument("--scenario", choices=["identical", "updated"], default="identical")
    p.add_argument("--json", help="Also write the result to this file")
    args = p.parse_args()

    for method_name, category in CATEGORIES.items():
        if hasattr(TableSegment, method_name):
            _wrap(method_name, category)

    shape = SHAPES[args.shape or ("oltp" if args.table.startswith("oltp_") else "fact")]
    conn = run_perf.connect(run_perf.URIS[args.target], shared=False)
    table_sql = conn.dialect.quote(args.table)
    where = shape["where"](conn.dialect.quote)
    cost = conn.dialect.quote(shape["update"])
    if args.scenario == "updated":
        conn.query(f"UPDATE {table_sql} SET {cost} = {cost} + 1 WHERE {where}", None)
        conn.query(commit)
    try:
        tables = [
            connect_to_table(
                run_perf.URIS[name],
                args.table,
                shape["key"],
                thread_count=run_perf.THREADS,
                extra_columns=shape["extra"],
            )
            for name in (args.source, args.target)
        ]
        start = time.monotonic()
        result_iter = diff_tables(
            *tables,
            algorithm=run_perf.ALGORITHM,
            bisection_factor=args.bisection_factor,
            bisection_threshold=args.bisection_threshold,
        )
        diff = list(result_iter)
        wall = time.monotonic() - start
        rows_downloaded = result_iter.stats.get("rows_downloaded", 0)
        for segment in tables:
            segment.database.close()
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
        bisection_factor=args.bisection_factor,
        bisection_threshold=args.bisection_threshold,
        rows_downloaded=rows_downloaded,
        checksum_queries=sum(c["calls"] for c in result["categories"] if c["category"] == "checksum"),
    )
    print(
        f"{args.source} -> {args.target} {args.table} {args.scenario} {run_perf.ALGORITHM}: "
        f"{wall:.1f}s wall, {len(diff)} diff lines, {run_perf.THREADS} threads, bisection factor "
        f"{args.bisection_factor} threshold {args.bisection_threshold}: {result['checksum_queries']} checksum queries "
        f"(2 per segment), {rows_downloaded} rows downloaded"
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
