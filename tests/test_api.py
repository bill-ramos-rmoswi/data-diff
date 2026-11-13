from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from data_diff import diff_tables, connect_to_table, Algorithm, derived_threadpool_size
from data_diff.databases.mysql import MySQL
from data_diff.databases.postgresql import PostgreSQL
from data_diff.queries.api import table, commit

from tests.common import (
    TEST_MYSQL_CONN_STRING,
    TEST_POSTGRESQL_CONN_STRING,
    get_conn,
    random_table_suffix,
    DiffTestCase,
    drop_table,
)


class TestApi(DiffTestCase):
    src_schema = {"id": int, "datetime": datetime, "text_comment": str}
    db_cls = MySQL

    def setUp(self) -> None:
        super().setUp()

        self.conn = self.connection

        self.now = now = datetime.now()

        rows = [
            (now, "now"),
            (self.now - timedelta(seconds=10), "a"),
            (self.now - timedelta(seconds=7), "b"),
            (self.now - timedelta(seconds=6), "c"),
        ]

        self.conn.query(
            [
                self.src_table.insert_rows((i, ts, s) for i, (ts, s) in enumerate(rows)),
                self.dst_table.create(self.src_table),
                self.src_table.insert_row(len(rows), self.now - timedelta(seconds=3), "3 seconds ago"),
                commit,
            ]
        )

    def test_api(self):
        # test basic
        t1 = connect_to_table(TEST_MYSQL_CONN_STRING, self.table_src_name)
        t2 = connect_to_table(TEST_MYSQL_CONN_STRING, (self.table_dst_name,))
        diff = list(diff_tables(t1, t2, algorithm=Algorithm.JOINDIFF))
        assert len(diff) == 1

        # test algorithm
        # (also tests shared connection on connect_to_table)
        for algo in (Algorithm.HASHDIFF, Algorithm.JOINDIFF):
            diff = list(diff_tables(t1, t2, algorithm=algo))
            assert len(diff) == 1

        # test where
        diff_id = diff[0][1][0]
        where = f"id != {diff_id} OR id = 90000000"

        t1 = connect_to_table(TEST_MYSQL_CONN_STRING, self.table_src_name, where=where)
        t2 = connect_to_table(TEST_MYSQL_CONN_STRING, self.table_dst_name, where=where)
        diff = list(diff_tables(t1, t2))
        assert len(diff) == 0

    def test_api_get_stats_dict(self):
        # XXX Likely to change in the future
        expected_dict = {
            "rows_A": 5,
            "rows_B": 4,
            "exclusive_A": 1,
            "exclusive_B": 0,
            "updated": 0,
            "unchanged": 4,
            "total": 1,
            "values": {},
        }
        t1 = connect_to_table(TEST_MYSQL_CONN_STRING, self.table_src_name)
        t2 = connect_to_table(TEST_MYSQL_CONN_STRING, self.table_dst_name)
        diff = diff_tables(t1, t2)

        output = diff.get_stats_dict()
        output.pop("stats")
        self.assertEqual(expected_dict, output)
        self.assertIsNotNone(diff)
        assert len(list(diff)) == 1


class TestDerivedThreadPool(unittest.TestCase):
    """diff_tables() runs both sides, and several segments, at once unless it's told otherwise."""

    def test_pool_size_follows_the_thread_counts(self):
        def segment(thread_count):
            return SimpleNamespace(database=SimpleNamespace(thread_count=thread_count))

        self.assertEqual(2, derived_threadpool_size(segment(1), segment(1)))
        self.assertEqual(8, derived_threadpool_size(segment(4), segment(2)))
        # A database without its own pool, such as DuckDB
        self.assertEqual(2, derived_threadpool_size(SimpleNamespace(database=SimpleNamespace()), segment(1)))

    def test_same_differences_as_one_query_at_a_time(self):
        suffix = random_table_suffix()
        rows = [(i, f"value {i}") for i in range(1, 2001)]
        changed = [(i, f"changed {i}") if i % 97 == 0 else (i, v) for i, v in rows if i % 251]
        conns = {MySQL: get_conn(MySQL), PostgreSQL: get_conn(PostgreSQL)}
        names = {MySQL: f"src{suffix}", PostgreSQL: f"dst{suffix}"}
        for (cls, conn), data in zip(conns.items(), (rows, changed)):
            t = table(names[cls], schema={"id": int, "v": str})
            conn.query([t.create(), t.insert_rows(data), commit])
        try:
            uris = {MySQL: TEST_MYSQL_CONN_STRING, PostgreSQL: TEST_POSTGRESQL_CONN_STRING}
            results = []
            for pool in ({}, {"max_threadpool_size": 1}):
                segments = [
                    connect_to_table(uris[cls], names[cls], "id", thread_count=4, extra_columns=("v",))
                    for cls in (MySQL, PostgreSQL)
                ]
                try:
                    diff = list(
                        diff_tables(
                            *segments, algorithm=Algorithm.HASHDIFF, bisection_factor=4, bisection_threshold=64, **pool
                        )
                    )
                finally:
                    # An open PostgreSQL transaction would block DROP TABLE
                    for s in segments:
                        s.database.close()
                results.append(diff)
            self.assertEqual(sorted(results[0]), sorted(results[1]))
            removed = 2000 // 251
            updated = len([i for i, _ in rows if i % 97 == 0 and i % 251])
            self.assertEqual(removed + 2 * updated, len(results[0]))
        finally:
            for cls, conn in conns.items():
                drop_table(conn, (names[cls],))
