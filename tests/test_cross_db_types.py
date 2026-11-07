"""Diffs column types where Db2, SQL Server and PostgreSQL disagree about precision and limits.

Each test pairs the kinds of values that a copy between these databases would really produce. Db2
truncates extra digits, while SQL Server and PostgreSQL round, so a correct copy can differ in
the last digit it stored. The tests also check that a real difference is still reported.
"""
from datetime import datetime, timedelta, timezone
import unittest

from data_diff import connect_to_table, diff_tables
from data_diff import databases as db
from data_diff.abcs.database_types import Native_UUID
from data_diff.databases.base import QueryError
from data_diff.databases.db2 import Dialect as Db2Dialect
from data_diff.databases.mssql import Dialect as MsSQLDialect
from data_diff.databases.postgresql import PostgresqlDialect
from data_diff.diff_tables import Algorithm
from data_diff.queries.api import commit
from data_diff.utils import ArithUUID
from tests.common import CONN_STRINGS, get_conn, random_table_suffix


def requires(*classes):
    return unittest.skipUnless(all(cls in CONN_STRINGS for cls in classes), "Needs a configured database")


class TypeTestCase(unittest.TestCase):
    def setUp(self):
        self.suffix = random_table_suffix()
        self.created = []

    def tearDown(self):
        for conn, table_sql in self.created:
            conn.query(f"DROP TABLE IF EXISTS {table_sql}", None)
            conn.query(commit)

    def table(self, db_cls, column_type, *values, name="value_test"):
        """Creates a table of (id, v) with ids from 1, and returns its name."""
        conn = get_conn(db_cls)
        table_name = f"{name}{self.suffix}"
        table_sql = conn.dialect.quote(table_name)
        conn.query(
            f"CREATE TABLE {table_sql} ({conn.dialect.quote('id')} INT NOT NULL PRIMARY KEY, "
            f"{conn.dialect.quote('v')} {column_type})",
            None,
        )
        self.created.append((conn, table_sql))
        for i, value in enumerate(values, 1):
            conn.query(f"INSERT INTO {table_sql} VALUES ({i}, {value})", None)
        conn.query(commit)
        return table_name

    def diff(self, a, b, **kwargs):
        (class_a, table_a), (class_b, table_b) = a, b
        options = {"extra_columns": ("v",), "thread_count": 2}
        segments = [
            connect_to_table(CONN_STRINGS[class_a], table_a, ("id",), **options),
            connect_to_table(CONN_STRINGS[class_b], table_b, ("id",), **options),
        ]
        kwargs.setdefault("algorithm", Algorithm.HASHDIFF)
        try:
            return sorted(diff_tables(*segments, bisection_factor=2, bisection_threshold=4, **kwargs), key=str)
        finally:
            for segment in segments:
                segment.database.close()

    def assert_both_directions(self, a, b, count=0):
        self.assertEqual(count, len(self.diff(a, b)))
        self.assertEqual(count, len(self.diff(b, a)))


@requires(db.MsSQL, db.Db2)
class TestMsSQLAgainstDb2(TypeTestCase):
    def test_datetime2_matches_a_truncated_timestamp(self):
        ms = self.table(db.MsSQL, "DATETIME2(7)", "'2026-01-02 03:04:05.1234567'", "'2026-01-02 03:04:05.1234564'")
        db2 = self.table(db.Db2, "TIMESTAMP(6)", "'2026-01-02-03.04.05.123456'", "'2026-01-02-03.04.05.123456'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))

    def test_timestamp_with_a_seventh_digit(self):
        ms = self.table(db.MsSQL, "DATETIME2(7)", "'2026-01-02 03:04:05.1234567'")
        db2 = self.table(db.Db2, "TIMESTAMP(7)", "'2026-01-02-03.04.05.1234567'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))

    def test_timestamp_above_seven_digits(self):
        ms = self.table(db.MsSQL, "DATETIME2(7)", "'2026-01-02 03:04:05.1234567'")
        db2 = self.table(db.Db2, "TIMESTAMP(9)", "'2026-01-02-03.04.05.123456700'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))

    def test_real_timestamp_difference_is_reported(self):
        ms = self.table(db.MsSQL, "DATETIME2(7)", "'2026-01-02 03:04:05.1234567'")
        db2 = self.table(db.Db2, "TIMESTAMP(6)", "'2026-01-02-03.04.05.123455'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2), count=2)

    def test_time_matches_at_whole_seconds(self):
        ms = self.table(db.MsSQL, "TIME(7)", "'12:00:00.5000000'", "'12:00:00.9999999'")
        db2 = self.table(db.Db2, "TIME", "'12:00:00'", "'12:00:00'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))

    def test_end_of_time_timestamp_can_round(self):
        ms = self.table(db.MsSQL, "DATETIME2(3)", "'9999-12-31 23:59:59.999'")
        db2 = self.table(db.Db2, "TIMESTAMP(6)", "'9999-12-31-23.59.59.999999'")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))

    def test_large_float(self):
        ms = self.table(db.MsSQL, "FLOAT", "1e18", "-0.5")
        db2 = self.table(db.Db2, "DOUBLE", "1e18", "-0.5")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2))


@requires(db.PostgreSQL, db.Db2)
class TestPostgreSQLAgainstDb2(TypeTestCase):
    def test_longer_timestamp_rounds_like_postgresql(self):
        db2 = self.table(db.Db2, "TIMESTAMP(9)", "'2026-01-02-03.04.05.123456700'", "'2026-01-02-03.04.05.123456400'")
        pg = self.table(db.PostgreSQL, "TIMESTAMP(6)", "'2026-01-02 03:04:05.123457'", "'2026-01-02 03:04:05.123456'")
        self.assert_both_directions((db.Db2, db2), (db.PostgreSQL, pg))

    def test_real_timestamp_difference_is_reported(self):
        db2 = self.table(db.Db2, "TIMESTAMP(9)", "'2026-01-02-03.04.05.123456700'")
        pg = self.table(db.PostgreSQL, "TIMESTAMP(6)", "'2026-01-02 03:04:05.123456'")
        self.assert_both_directions((db.Db2, db2), (db.PostgreSQL, pg), count=2)

    def test_time_matches_at_whole_seconds(self):
        pg = self.table(db.PostgreSQL, "TIME(6)", "'12:00:00.5'", "'12:00:00.999999'")
        db2 = self.table(db.Db2, "TIME", "'12:00:00'", "'12:00:00'")
        self.assert_both_directions((db.PostgreSQL, pg), (db.Db2, db2))

    def test_real_time_difference_is_reported(self):
        pg = self.table(db.PostgreSQL, "TIME(6)", "'12:00:01'")
        db2 = self.table(db.Db2, "TIME", "'12:00:00'")
        self.assert_both_directions((db.PostgreSQL, pg), (db.Db2, db2), count=2)

    def test_float_edge_values(self):
        values = "1e18", "1.5e20", "-0.5", "0.32", "0", "1e-10", "-1e-20", "NULL"
        pg = self.table(db.PostgreSQL, "DOUBLE PRECISION", *values)
        db2 = self.table(db.Db2, "DOUBLE", *values)
        self.assert_both_directions((db.PostgreSQL, pg), (db.Db2, db2))

    def test_clob_within_the_hash_limit(self):
        text = "'" + "a" * 2000 + "'"
        pg = self.table(db.PostgreSQL, "TEXT", text)
        db2 = self.table(db.Db2, "CLOB(100K)", text)
        self.assert_both_directions((db.PostgreSQL, pg), (db.Db2, db2))


@requires(db.Db2)
class TestDb2(TypeTestCase):
    def test_huge_floats_stay_distinct(self):
        a = self.table(db.Db2, "DOUBLE", "1e30", name="value_a")
        b = self.table(db.Db2, "DOUBLE", "2e30", name="value_b")
        self.assertEqual(2, len(self.diff((db.Db2, a), (db.Db2, b))))

    def test_decfloat34(self):
        a = self.table(db.Db2, "DECFLOAT(34)", "0.5", "1234567.25", name="value_a")
        b = self.table(db.Db2, "DECFLOAT(34)", "0.5", "1234567.25", name="value_b")
        self.assertEqual([], self.diff((db.Db2, a), (db.Db2, b)))

    def test_equal_decfloats_with_different_trailing_zeros(self):
        a = self.table(db.Db2, "DECFLOAT(34)", "DECFLOAT('1234567.25')", name="value_a")
        b = self.table(db.Db2, "DECFLOAT(34)", "DECFLOAT('1234567.250')", name="value_b")
        self.assertEqual([], self.diff((db.Db2, a), (db.Db2, b)))

    def test_decfloat_nan_and_infinity(self):
        values = "DECFLOAT('NaN')", "DECFLOAT('Infinity')"
        a = self.table(db.Db2, "DECFLOAT(16)", *values, name="value_a")
        b = self.table(db.Db2, "DECFLOAT(16)", *values, name="value_b")
        self.assertEqual([], self.diff((db.Db2, a), (db.Db2, b)))

    def test_long_clob_fails_with_an_explanation(self):
        long_text = "CLOB(REPEAT('a', 32000)) || CLOB(REPEAT('a', 8000))"
        a = self.table(db.Db2, "CLOB(100K)", long_text, name="value_a")
        b = self.table(db.Db2, "CLOB(100K)", long_text, name="value_b")
        with self.assertRaisesRegex(QueryError, "32,672 bytes"):
            self.diff((db.Db2, a), (db.Db2, b))


class JoinDiffCases:
    def test_compound_keys_that_concatenate_equally(self):
        for name in ("key_a", "key_b"):
            conn = get_conn(self.db_cls)
            table_name = f"{name}{self.suffix}"
            table_sql = conn.dialect.quote(table_name)
            q = conn.dialect.quote
            conn.query(
                f"CREATE TABLE {table_sql} ({q('a')} INT NOT NULL, {q('b')} INT NOT NULL, {q('v')} INT, "
                f"PRIMARY KEY ({q('a')}, {q('b')}))",
                None,
            )
            self.created.append((conn, table_sql))
            conn.query(f"INSERT INTO {table_sql} VALUES (1, 11, 5), (11, 1, 6)", None)
            conn.query(commit)

        options = {"extra_columns": ("v",), "thread_count": 2}
        uri = CONN_STRINGS[self.db_cls]
        segments = [connect_to_table(uri, f"{n}{self.suffix}", ("a", "b"), **options) for n in ("key_a", "key_b")]
        try:
            self.assertEqual([], list(diff_tables(*segments, algorithm=Algorithm.JOINDIFF)))
        finally:
            for segment in segments:
                segment.database.close()

    def test_materialize_long_text(self):
        conn = get_conn(self.db_cls)
        a = self.table(self.db_cls, self.long_text_type, f"'{'a' * 5000}'", name="value_a")
        b = self.table(self.db_cls, self.long_text_type, f"'{'b' * 5000}'", name="value_b")
        output = f"materialized{self.suffix}"
        self.created.append((conn, conn.dialect.quote(output)))
        pair = (self.db_cls, a), (self.db_cls, b)
        self.assertEqual(2, len(self.diff(*pair, algorithm=Algorithm.JOINDIFF, materialize_to_table=(output,))))
        self.assertEqual(1, conn.query(f"SELECT COUNT(*) FROM {conn.dialect.quote(output)}", int))

    def test_sample_exclusive_rows(self):
        a = self.table(self.db_cls, "INT", "1", "2", name="value_a")
        b = self.table(self.db_cls, "INT", "1", name="value_b")
        pair = (self.db_cls, a), (self.db_cls, b)
        self.assertEqual(1, len(self.diff(*pair, algorithm=Algorithm.JOINDIFF, sample_exclusive_rows=True)))


@requires(db.MsSQL)
class TestJoinDiffMsSQL(JoinDiffCases, TypeTestCase):
    db_cls = db.MsSQL
    long_text_type = "NVARCHAR(MAX)"


@requires(db.Db2)
class TestJoinDiffDb2(JoinDiffCases, TypeTestCase):
    db_cls = db.Db2
    long_text_type = "VARCHAR(8000)"


class TestValues(unittest.TestCase):
    def test_db2_converts_a_timestamp_to_utc(self):
        value = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual("TIMESTAMP('2024-01-01 05:00:00')", Db2Dialect().timestamp_value(value))

    def test_other_databases_convert_a_timestamp_to_utc(self):
        value = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=-5)))
        for dialect in (MsSQLDialect(), PostgresqlDialect()):
            self.assertEqual("'2024-01-01T05:00:00+00:00'", dialect.timestamp_value(value))

    def test_db2_keeps_a_timestamp_without_a_time_zone(self):
        self.assertEqual("TIMESTAMP('2024-01-01 00:00:00')", Db2Dialect().timestamp_value(datetime(2024, 1, 1)))

    def test_native_uuid_bound_has_no_case(self):
        value = ArithUUID("a1b2c3d4-0000-0000-0000-000000000001", uppercase=True)
        self.assertFalse(Native_UUID().make_value(value).uppercase)
