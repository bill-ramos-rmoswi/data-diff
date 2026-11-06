"""Times data-diff between each pair of databases, and checks that the result is exactly right.

Run this in the devcontainer, after dev/perf/seed.py:

    poetry run python dev/perf/run_perf.py fact_2x fact_13x

Each pair runs three scenarios, which change the target table and then restore it: identical
tables, updated rows, and deleted rows. A run fails if data-diff doesn't report exactly the rows
that were changed. Results go to benchmark_<revision>.jsonl, which dev/graph.py can chart.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from data_diff import connect, connect_to_table, diff_tables
from data_diff.queries.api import commit
from data_diff.utils import number_to_human

KEY_COLUMNS = ("ScaleKey", "ProductKey", "DateKey")
EXTRA_COLUMNS = ("MovementDate", "UnitCost", "UnitsIn", "UnitsOut", "UnitsBalance")
THREADS = int(os.environ.get("N_THREADS", 4))

URIS = {
    "PostgreSQL": os.environ["DATADIFF_POSTGRESQL_URI"],
    "MsSQL": os.environ["DATADIFF_MSSQL_URI"],
    "Db2": os.environ["DATADIFF_DB2_URI"],
}
PAIRS = [("PostgreSQL", "MsSQL"), ("PostgreSQL", "Db2"), ("MsSQL", "Db2")]

# One date, for a fifth of the products, in every ScaleKey. Spreads about 0.016% of rows across the table.
PRODUCT_KEYS = ", ".join(str(key) for key in range(5, 610, 5))
SAVED_TABLE = "perf_saved"


def changed_rows(conn, table_sql: str) -> str:
    quote = conn.dialect.quote
    return f"{table_sql} WHERE {quote('DateKey')} = 20130615 AND {quote('ProductKey')} IN ({PRODUCT_KEYS})"


def changed_keys(conn, table_sql: str) -> set:
    columns = ", ".join(conn.dialect.quote(column) for column in KEY_COLUMNS)
    return {tuple(str(value) for value in row) for row in conn.query(f"SELECT {columns} FROM {changed_rows(conn, table_sql)}", list)}


def save_rows(conn, table_sql: str):
    query = f"SELECT * FROM {changed_rows(conn, table_sql)}"
    if conn.name == "MsSQL":
        conn.query(f"SELECT * INTO {SAVED_TABLE} FROM {changed_rows(conn, table_sql)}", None)
    elif conn.name == "Db2":
        conn.query(f"CREATE TABLE {SAVED_TABLE} AS ({query}) WITH DATA", None)
    else:
        conn.query(f"CREATE TABLE {SAVED_TABLE} AS {query}", None)


def restore_rows(conn, table_sql: str):
    conn.query(f"INSERT INTO {table_sql} SELECT * FROM {SAVED_TABLE}", None)
    conn.query(f"DROP TABLE {SAVED_TABLE}", None)


def run_diff(source: str, target: str, table: str):
    tables = [
        connect_to_table(URIS[db_name], table, KEY_COLUMNS, thread_count=THREADS, extra_columns=EXTRA_COLUMNS)
        for db_name in (source, target)
    ]
    start = time.monotonic()
    diff = list(diff_tables(*tables))
    duration = time.monotonic() - start
    for segment in tables:
        segment.database.close()
    return diff, duration


def check(scenario: str, diff: list, expected_keys: set, signs: set):
    actual_keys = {row[: len(KEY_COLUMNS)] for _, row in diff}
    expected_lines = len(expected_keys) * len(signs)
    if actual_keys != expected_keys or len(diff) != expected_lines or {sign for sign, _ in diff} != signs:
        raise AssertionError(
            f"{scenario}: expected {expected_lines} lines for {len(expected_keys)} keys, got {len(diff)} lines "
            f"for {len(actual_keys)} keys, with {len(actual_keys ^ expected_keys)} keys not matching"
        )


def run_pair(source: str, target: str, table: str, row_count: int, revision: str):
    conn = connect(URIS[target], shared=False)
    table_sql = conn.dialect.quote(table)
    quote = conn.dialect.quote
    keys = changed_keys(conn, table_sql)

    def record(scenario: str, duration: float, lines: int):
        print(f"{source:>10} -> {target:<10} {table:<9} {scenario:<9} {duration:8.1f}s  {lines} lines", flush=True)
        result = {
            "test": f"perf_{source}_{target}_{scenario}".lower(),
            "source_db": source,
            "target_db": target,
            "date": str(datetime.today()),
            "git_revision": revision,
            "rows": row_count,
            "rows_human": number_to_human(row_count),
            "name_human": f"{source}/{target} {scenario}",
            "src_table": table,
            "target_table": table,
            "source_type": "fact",
            "target_type": scenario,
            "count_max_sec": 0,
            "checksum_sec": round(duration, 3),
            "download_sec": 0,
            "changed_rows": len(keys) if scenario != "identical" else 0,
            "threads": THREADS,
        }
        with open(f"benchmark_{revision}.jsonl", "a", encoding="utf-8") as file:
            file.write(json.dumps(result) + "\n")

    diff, duration = run_diff(source, target, table)
    check("identical", diff, set(), set())
    record("identical", duration, len(diff))

    cost = quote("UnitCost")
    conn.query(f"UPDATE {table_sql} SET {cost} = {cost} + 1 WHERE {changed_rows(conn, table_sql).split(' WHERE ')[1]}", None)
    conn.query(commit)
    try:
        diff, duration = run_diff(source, target, table)
        check("updated", diff, keys, {"-", "+"})
        record("updated", duration, len(diff))
    finally:
        conn.query(
            f"UPDATE {table_sql} SET {cost} = {cost} - 1 WHERE {changed_rows(conn, table_sql).split(' WHERE ')[1]}",
            None,
        )
        conn.query(commit)

    save_rows(conn, table_sql)
    conn.query(f"DELETE FROM {changed_rows(conn, table_sql)}", None)
    conn.query(commit)
    try:
        diff, duration = run_diff(source, target, table)
        check("deleted", diff, keys, {"-"})
        record("deleted", duration, len(diff))
    finally:
        restore_rows(conn, table_sql)
        conn.query(commit)
    conn.close()


def main():
    revision = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    tables = sys.argv[1:] or ["fact_2x"]
    for table in tables:
        conn = connect(URIS["PostgreSQL"], shared=False)
        row_count = conn.query(f"SELECT count(*) FROM {table}", int)
        conn.close()
        print(f"{table}: {row_count:,} rows, {THREADS} threads per database", flush=True)
        for source, target in PAIRS:
            run_pair(source, target, table, row_count, revision)


if __name__ == "__main__":
    main()
