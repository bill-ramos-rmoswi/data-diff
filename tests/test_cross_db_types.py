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
from data_diff.databases.sybase import Dialect as SybaseDialect
from data_diff.diff_tables import Algorithm
from data_diff.hashdiff_tables import HashDiffer
from data_diff.queries.api import commit, table
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
            if isinstance(conn, db.Sybase):
                # ASE has no DROP TABLE IF EXISTS
                conn.query(table(table_sql.strip("[]")).drop(if_exists=True))
            else:
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

    def checksums(self, a, b):
        """Count and checksum of each whole table, at the precision a diff would use."""
        (class_a, table_a), (class_b, table_b) = a, b
        segments = [
            connect_to_table(CONN_STRINGS[cls], name, ("id",), extra_columns=("v",), thread_count=2)
            for cls, name in ((class_a, table_a), (class_b, table_b))
        ]
        try:
            segments = [segment.with_schema() for segment in segments]
            HashDiffer()._validate_and_adjust_columns(*segments)
            return [segment.count_and_checksum() for segment in segments]
        finally:
            for segment in segments:
                segment.database.close()

    def assert_matches(self, a, b):
        """Equal checksums in both directions, so the rows are never downloaded to compare them."""
        for x, y in ((a, b), (b, a)):
            first, second = self.checksums(x, y)
            self.assertEqual(first, second, f"{x[0].__name__} -> {y[0].__name__}")
        self.assert_both_directions(a, b)


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
            conn.query(f"INSERT INTO {table_sql} VALUES (1, 11, 5)", None)
            conn.query(f"INSERT INTO {table_sql} VALUES (11, 1, 6)", None)
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


# Sybase ASE. The server's character set is cp850, so text outside cp850 is sent as U&'' escapes.
CP850_HIGH = bytes(range(128, 256)).decode("cp850")


def ase(value) -> str:
    """An ASE literal for a Python string, or NULL."""
    return "NULL" if value is None else SybaseDialect()._constant_value(value)


def pg(value) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def ms(value) -> str:
    return "NULL" if value is None else "N'" + value.replace("'", "''") + "'"


TEXTS = ["plain", "café", "Ünïcödé", CP850_HIGH[:60], CP850_HIGH[60:], "it's", "a|b", "<null>", None]
UNICODE_TEXTS = TEXTS + ["日本語", "emoji \U0001f600", "mix é日\U0001f600z", "\U0010fffd", "ࠀ߿"]


@requires(db.Sybase, db.PostgreSQL)
class TestSybaseAgainstPostgreSQL(TypeTestCase):
    def test_cp850_text(self):
        a = self.table(db.Sybase, "VARCHAR(100)", *map(ase, TEXTS))
        b = self.table(db.PostgreSQL, "VARCHAR(100)", *map(pg, TEXTS))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_real_text_difference_is_reported(self):
        a = self.table(db.Sybase, "VARCHAR(100)", ase("café"), ase("Ünïcödé"))
        b = self.table(db.PostgreSQL, "VARCHAR(100)", pg("cafe"), pg("Ünïcödè"))
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=4)

    def test_unicode_text(self):
        a = self.table(db.Sybase, "UNIVARCHAR(100)", *map(ase, UNICODE_TEXTS))
        b = self.table(db.PostgreSQL, "VARCHAR(100)", *map(pg, UNICODE_TEXTS))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_unicode_characters_that_convert_to_the_same_cp850_differ(self):
        # Converting to cp850 turns both into '?', which must not make them equal
        a = self.table(db.Sybase, "UNIVARCHAR(10)", ase("日"), ase("?"))
        b = self.table(db.PostgreSQL, "VARCHAR(10)", pg("本"), pg("日"))
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=4)

    def test_long_unicode_text_in_cp850(self):
        # Longer than the per-character limit, but it fits in cp850, which has no such limit
        text = "é" * 1500
        a = self.table(db.Sybase, "UNIVARCHAR(1500)", ase(text))
        b = self.table(db.PostgreSQL, "VARCHAR(1500)", pg(text))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_long_text_with_trailing_spaces(self):
        text = "x" * 5000 + "  "
        a = self.table(db.Sybase, "TEXT", ase(text), ase("abc "), ase("abc"))
        b = self.table(db.PostgreSQL, "TEXT", pg(text), pg("abc "), pg("abc"))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_trailing_space_difference_in_text_is_reported(self):
        a = self.table(db.Sybase, "TEXT", ase("abc "))
        b = self.table(db.PostgreSQL, "TEXT", pg("abc"))
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_unitext(self):
        values = ["abc ", "日本語 ", "é" * 3000]
        a = self.table(db.Sybase, "UNITEXT", *map(ase, values))
        b = self.table(db.PostgreSQL, "TEXT", *map(pg, values))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_padded_char(self):
        a = self.table(db.Sybase, "CHAR(5) NOT NULL", ase("ab"), ase("abcde"))
        b = self.table(db.PostgreSQL, "CHAR(5)", pg("ab"), pg("abcde"))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_empty_string_is_a_space(self):
        # ASE stores '' as ' ', which is a real difference from ''
        a = self.table(db.Sybase, "VARCHAR(10)", "''")
        b = self.table(db.PostgreSQL, "VARCHAR(10)", "''")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_varchar_drops_trailing_spaces(self):
        a = self.table(db.Sybase, "VARCHAR(10)", ase("a  "))
        b = self.table(db.PostgreSQL, "VARCHAR(10)", pg("a  "))
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_numeric_and_money(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "-0.0001", "NULL"
        for ase_type in ("NUMERIC(19, 4)", "MONEY"):
            a = self.table(db.Sybase, ase_type, *values, name=f"value_{ase_type[:5]}")
            b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", *values, name=f"value_pg_{ase_type[:5]}")
            self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_numeric_at_a_lower_scale_rounds(self):
        a = self.table(db.Sybase, "NUMERIC(10, 3)", "1.125", "-1.125", "2.004")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 2)", "1.13", "-1.13", "2.00")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_real_numeric_difference_is_reported(self):
        a = self.table(db.Sybase, "MONEY", "1131.2500")
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", "1131.2501")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_float_edge_values(self):
        values = "1e18", "1.5e20", "-0.5", "0.32", "0", "1e-10", "-1e-20", "123456789012.3456789", "1e23", "NULL"
        a = self.table(db.Sybase, "FLOAT", *values)
        b = self.table(db.PostgreSQL, "DOUBLE PRECISION", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_real(self):
        values = "0.1", "0.333333343267", "-2.5e10", "123.456"
        a = self.table(db.Sybase, "REAL", *values)
        b = self.table(db.PostgreSQL, "REAL", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_real_float_difference_is_reported(self):
        a = self.table(db.Sybase, "FLOAT", "0.32")
        b = self.table(db.PostgreSQL, "DOUBLE PRECISION", "0.33")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_bigdatetime(self):
        values = "'2026-01-02 03:04:05.123456'", "'0001-01-01 00:00:00'", "'9999-12-31 23:59:59.999999'", "NULL"
        a = self.table(db.Sybase, "BIGDATETIME", *values)
        b = self.table(db.PostgreSQL, "TIMESTAMP(6)", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_end_of_time_can_round(self):
        a = self.table(db.Sybase, "BIGDATETIME", "'9999-12-31 23:59:59.999999'")
        b = self.table(db.PostgreSQL, "TIMESTAMP(3)", "'9999-12-31 23:59:59.999'")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_bigdatetime_at_a_lower_precision_rounds(self):
        a = self.table(db.Sybase, "BIGDATETIME", "'2026-01-02 03:04:05.123456'", "'2026-01-02 03:04:05.999600'")
        b = self.table(db.PostgreSQL, "TIMESTAMP(3)", "'2026-01-02 03:04:05.123'", "'2026-01-02 03:04:06'")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_datetime_copied_through_ase(self):
        # ASE stores 1/300 second ticks and shows them truncated to milliseconds, which is what a
        # copy made through ASE holds. .998 is stored as tick 299, .996667.
        values_ase = "'2026-01-02 03:04:05.998'", "'2026-01-02 03:04:05.001'", "'2026-01-02 03:04:05'"
        values_pg = "'2026-01-02 03:04:05.996'", "'2026-01-02 03:04:05.000'", "'2026-01-02 03:04:05'"
        a = self.table(db.Sybase, "DATETIME", *values_ase)
        b = self.table(db.PostgreSQL, "TIMESTAMP(6)", *values_pg)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_real_datetime_difference_is_reported(self):
        a = self.table(db.Sybase, "DATETIME", "'2026-01-02 03:04:05.998'")
        b = self.table(db.PostgreSQL, "TIMESTAMP(6)", "'2026-01-02 03:04:05.993'")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_smalldatetime(self):
        a = self.table(db.Sybase, "SMALLDATETIME", "'2026-01-02 03:04:29.999'", "'2026-01-02 03:04:30'")
        b = self.table(db.PostgreSQL, "TIMESTAMP(0)", "'2026-01-02 03:04:00'", "'2026-01-02 03:05:00'")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_date(self):
        values = "'2026-01-02'", "'0001-01-01'", "'9999-12-31'", "NULL"
        a = self.table(db.Sybase, "DATE", *values)
        b = self.table(db.PostgreSQL, "DATE", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_bigtime_and_time(self):
        a = self.table(db.Sybase, "BIGTIME", "'12:00:00.123456'", "'23:59:59.999999'")
        b = self.table(db.PostgreSQL, "TIME(6)", "'12:00:00.123456'", "'23:59:59.999999'")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))
        c = self.table(db.Sybase, "TIME", "'12:00:00.998'", "'23:59:59.999'", name="value_time")
        d = self.table(db.PostgreSQL, "TIME(6)", "'12:00:00.996'", "'23:59:59.996'", name="value_pg_time")
        self.assert_matches((db.Sybase, c), (db.PostgreSQL, d))

    def test_real_time_difference_is_reported(self):
        a = self.table(db.Sybase, "BIGTIME", "'12:00:00.123456'")
        b = self.table(db.PostgreSQL, "TIME(6)", "'12:00:00.123457'")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_integers(self):
        a = self.table(db.Sybase, "UNSIGNED INT", "4294967295", "0", "2147483648")
        b = self.table(db.PostgreSQL, "BIGINT", "4294967295", "0", "2147483648")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))
        c = self.table(db.Sybase, "NUMERIC(20, 0)", "18446744073709551615", "-5", name="value_num")
        d = self.table(db.PostgreSQL, "NUMERIC(20, 0)", "18446744073709551615", "-5", name="value_pg_num")
        self.assert_matches((db.Sybase, c), (db.PostgreSQL, d))

    def test_bit(self):
        a = self.table(db.Sybase, "BIT NOT NULL", "1", "0")
        b = self.table(db.PostgreSQL, "BOOLEAN", "true", "false")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))
        c = self.table(db.Sybase, "BIT NOT NULL", "1", name="value_bit")
        d = self.table(db.PostgreSQL, "BOOLEAN", "false", name="value_pg_bit")
        self.assert_both_directions((db.Sybase, c), (db.PostgreSQL, d), count=2)

    def test_unsigned_int_keys(self):
        conn = get_conn(db.Sybase)
        names = []
        for cls, key_type in ((db.Sybase, "UNSIGNED INT"), (db.PostgreSQL, "BIGINT")):
            c = get_conn(cls)
            name = f"ukey_{cls.__name__.lower()}{self.suffix}"
            q = c.dialect.quote
            c.query(f"CREATE TABLE {q(name)} ({q('id')} {key_type} NOT NULL PRIMARY KEY, {q('v')} INT)", None)
            self.created.append((c, q(name)))
            for k in (1, 2147483647, 2147483648, 4294967295):
                c.query(f"INSERT INTO {q(name)} VALUES ({k}, {k % 7})", None)
            if cls is db.PostgreSQL:
                c.query(f"UPDATE {q(name)} SET {q('v')} = 99 WHERE {q('id')} = 4294967295", None)
            c.query(commit)
            names.append((cls, name))
        self.assertEqual([("+", ("4294967295", "99")), ("-", ("4294967295", str(4294967295 % 7)))], self.diff(*names))
        del conn


@requires(db.Sybase, db.MsSQL)
class TestSybaseAgainstMsSQL(TypeTestCase):
    def test_unicode_text(self):
        a = self.table(db.Sybase, "UNIVARCHAR(100)", *map(ase, UNICODE_TEXTS))
        b = self.table(db.MsSQL, "NVARCHAR(100)", *map(ms, UNICODE_TEXTS))
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_cp850_text(self):
        a = self.table(db.Sybase, "VARCHAR(100)", *map(ase, TEXTS))
        b = self.table(db.MsSQL, "NVARCHAR(100)", *map(ms, TEXTS))
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_datetime_ticks_match(self):
        # Both store 1/300 second ticks. SQL Server shows tick 299 as .997, and ASE as .996.
        values = (
            "'2026-01-02 03:04:05.998'",
            "'2026-01-02 03:04:05.001'",
            "'2026-01-02 03:04:05.005'",
            "'9999-12-31 23:59:59.997'",
        )
        a = self.table(db.Sybase, "DATETIME", *values)
        b = self.table(db.MsSQL, "DATETIME", *values)
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_real_datetime_difference_is_reported(self):
        a = self.table(db.Sybase, "DATETIME", "'2026-01-02 03:04:05.998'")
        # .995 isn't a difference: SQL Server rounds it to the same tick as .998
        b = self.table(db.MsSQL, "DATETIME", "'2026-01-02 03:04:05.990'")
        self.assert_both_directions((db.Sybase, a), (db.MsSQL, b), count=2)

    def test_money(self):
        values = "1131.2500", "-922337203685477.5808", "922337203685477.5807", "0.0001", "NULL"
        a = self.table(db.Sybase, "MONEY", *values)
        b = self.table(db.MsSQL, "MONEY", *values)
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_bigdatetime(self):
        values = "'2026-01-02 03:04:05.123456'", "'9999-12-31 23:59:59.999999'"
        a = self.table(db.Sybase, "BIGDATETIME", *values)
        b = self.table(db.MsSQL, "DATETIME2(6)", *values)
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_bigdatetime_against_seven_digits(self):
        a = self.table(db.Sybase, "BIGDATETIME", "'2026-01-02 03:04:05.123457'")
        b = self.table(db.MsSQL, "DATETIME2(7)", "'2026-01-02 03:04:05.1234567'")
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_float(self):
        values = "1e18", "-0.5", "0.32", "0", "1e-10", "3.141592653589793"
        a = self.table(db.Sybase, "FLOAT", *values)
        b = self.table(db.MsSQL, "FLOAT", *values)
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_bit(self):
        a = self.table(db.Sybase, "BIT NOT NULL", "1", "0")
        b = self.table(db.MsSQL, "BIT", "1", "0")
        self.assert_matches((db.Sybase, a), (db.MsSQL, b))


@requires(db.Sybase, db.Db2)
class TestSybaseAgainstDb2(TypeTestCase):
    def test_unicode_text(self):
        a = self.table(db.Sybase, "UNIVARCHAR(100)", *map(ase, UNICODE_TEXTS))
        b = self.table(db.Db2, "VARCHAR(400)", *map(pg, UNICODE_TEXTS))
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_cp850_text(self):
        a = self.table(db.Sybase, "VARCHAR(100)", *map(ase, TEXTS))
        b = self.table(db.Db2, "VARCHAR(400)", *map(pg, TEXTS))
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_numeric(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "NULL"
        a = self.table(db.Sybase, "MONEY", *values)
        b = self.table(db.Db2, "DECIMAL(19, 4)", *values)
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_datetime_copied_through_ase(self):
        a = self.table(db.Sybase, "DATETIME", "'2026-01-02 03:04:05.998'", "'2026-01-02 03:04:05.001'")
        b = self.table(db.Db2, "TIMESTAMP(3)", "'2026-01-02-03.04.05.996'", "'2026-01-02-03.04.05.000'")
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_bigdatetime(self):
        values = "'2026-01-02 03:04:05.123456'", "'9999-12-31 23:59:59.999999'"
        a = self.table(db.Sybase, "BIGDATETIME", *values)
        b = self.table(db.Db2, "TIMESTAMP(6)", "'2026-01-02-03.04.05.123456'", "'9999-12-31-23.59.59.999999'")
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_float(self):
        values = "1e18", "1.5e20", "-0.5", "0.32", "0", "1e-10", "-1e-20", "NULL"
        a = self.table(db.Sybase, "FLOAT", *values)
        b = self.table(db.Db2, "DOUBLE", *values)
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_real_difference_is_reported(self):
        a = self.table(db.Sybase, "UNIVARCHAR(10)", ase("日本"))
        b = self.table(db.Db2, "VARCHAR(40)", pg("日木"))
        self.assert_both_directions((db.Sybase, a), (db.Db2, b), count=2)


@requires(db.Sybase)
class TestSybase(TypeTestCase):
    def test_huge_floats_stay_distinct(self):
        a = self.table(db.Sybase, "FLOAT", "1e30", name="value_a")
        b = self.table(db.Sybase, "FLOAT", "2e30", name="value_b")
        self.assertEqual(2, len(self.diff((db.Sybase, a), (db.Sybase, b))))

    def test_long_unicode_text_fails_with_an_explanation(self):
        # More rows than the bisection threshold, so they're checksummed instead of downloaded
        values = [ase("日" * 300)] * 10
        a = self.table(db.Sybase, "UNIVARCHAR(300)", *values, name="value_a")
        b = self.table(db.Sybase, "UNIVARCHAR(300)", *values, name="value_b")
        with self.assertRaisesRegex(QueryError, "Unicode text over 255 characters"):
            self.diff((db.Sybase, a), (db.Sybase, b))

    def test_text_over_the_expression_limit_fails_with_an_explanation(self):
        conn = get_conn(db.Sybase)
        a = self.table(db.Sybase, "TEXT", "'x'", name="value_a")
        b = self.table(db.Sybase, "TEXT", "'x'", name="value_b")
        # Expressions stop at 16384 bytes, so the long value comes from the client
        conn.query(f"UPDATE {conn.dialect.quote(a)} SET v = '{'y' * 10000}'", None)
        conn._run_on_connection(
            lambda raw: raw.cursor().execute(f"UPDATE {conn.dialect.quote(b)} SET v = ?", "y" * 20000)
        )
        self.assertEqual(20000, conn.query(f"SELECT datalength(v) FROM {conn.dialect.quote(b)}", int))
        with self.assertRaisesRegex(QueryError, "text over 16384 bytes"):
            self.diff((db.Sybase, a), (db.Sybase, b))

    def test_rows_over_the_hash_limit_fail_with_an_explanation(self):
        conn = get_conn(db.Sybase)
        q = conn.dialect.quote
        names = []
        for name in ("row_a", "row_b"):
            table_name = f"{name}{self.suffix}"
            conn.query(f"CREATE TABLE {q(table_name)} (id INT NOT NULL PRIMARY KEY, v TEXT, w TEXT)", None)
            self.created.append((conn, q(table_name)))
            # More rows than the bisection threshold, so they're checksummed instead of downloaded
            for i in range(10):
                conn.query(f"INSERT INTO {q(table_name)} VALUES ({i}, '{'a' * 9000}', '{'b' * 9000}')", None)
            names.append(table_name)
        segments = [
            connect_to_table(CONN_STRINGS[db.Sybase], n, ("id",), extra_columns=("v", "w"), thread_count=2)
            for n in names
        ]
        try:
            with self.assertRaisesRegex(QueryError, "a row over 16384 bytes"):
                list(diff_tables(*segments, bisection_factor=2, bisection_threshold=4, algorithm=Algorithm.HASHDIFF))
        finally:
            for segment in segments:
                segment.database.close()

    def test_text_that_differs_after_its_trailing_spaces(self):
        a = self.table(db.Sybase, "TEXT", ase("abc "), name="value_a")
        b = self.table(db.Sybase, "TEXT", ase("abc"), name="value_b")
        self.assertEqual(2, len(self.diff((db.Sybase, a), (db.Sybase, b))))

    def test_char_of_spaces_is_not_null(self):
        a = self.table(db.Sybase, "CHAR(3) NULL", "'   '", name="value_a")
        b = self.table(db.Sybase, "CHAR(3) NULL", "NULL", name="value_b")
        self.assertEqual(2, len(self.diff((db.Sybase, a), (db.Sybase, b))))


@requires(db.Sybase)
class TestJoinDiffSybase(JoinDiffCases, TypeTestCase):
    db_cls = db.Sybase
    long_text_type = "TEXT"
