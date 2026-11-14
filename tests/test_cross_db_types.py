"""Diffs column types where Db2, SQL Server and PostgreSQL disagree about precision and limits.

Each test pairs the kinds of values that a copy between these databases would really produce. Db2
truncates extra digits, while SQL Server and PostgreSQL round, so a correct copy can differ in
the last digit it stored. The tests also check that a real difference is still reported.
"""
from datetime import datetime, timedelta, timezone
import unittest
import uuid

from data_diff import connect_to_table, diff_tables
from data_diff import databases as db
from data_diff.abcs.database_types import Decimal, Native_UUID
from data_diff.databases.base import QueryError
from data_diff.databases.db2 import Dialect as Db2Dialect
from data_diff.databases.mssql import Dialect as MsSQLDialect
from data_diff.databases.postgresql import PostgresqlDialect
from data_diff.databases.sybase import Dialect as SybaseDialect
from data_diff.diff_tables import Algorithm
from data_diff.hashdiff_tables import HashDiffer
from data_diff.queries.api import Count, commit, table, this
from data_diff.queries.extras import Checksum, NormalizeAsString
from data_diff.schema import RawColumnInfo
from data_diff.utils import ArithUUID, lob_digest
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

    def test_numeric_matches_decimal(self):
        values = "0", "12.3456789012", "-0.5", "-0.0000000001", "-0.9999", "999999999999999999999.9999999999", "NULL"
        ms = self.table(db.MsSQL, "NUMERIC(31, 10)", *values)
        db2 = self.table(db.Db2, "DECIMAL(31, 10)", *values)
        self.assert_matches((db.MsSQL, ms), (db.Db2, db2))

    def test_real_numeric_difference_is_reported(self):
        ms = self.table(db.MsSQL, "NUMERIC(31, 10)", "-0.0000000001", "1131.25")
        db2 = self.table(db.Db2, "DECIMAL(31, 10)", "-0.0000000002", "1131.25")
        self.assert_both_directions((db.MsSQL, ms), (db.Db2, db2), count=2)


@requires(db.MsSQL, db.PostgreSQL)
class TestMsSQLNumericAgainstPostgreSQL(TypeTestCase):
    """SQL Server `numeric` is the same type as `decimal`, and must be compared as a number."""

    VALUES = {
        "NUMERIC(38, 0)": ("0", "-1", "12", "99999999999999999999999999999999999999", "-" + "9" * 38, "NULL"),
        "NUMERIC(38, 4)": ("0", "12.34", "0.24", "-0.5", "-0.0001", "-0.9999", "9" * 34 + ".9999", "NULL"),
        "NUMERIC(38, 10)": ("0", "12.3456789012", "-0.5", "-0.0000000001", "-0.9999", "9" * 28 + ".9999999999", "NULL"),
        "NUMERIC(5, 4)": ("0", "0.24", "-0.5", "-0.0001", "-0.9999", "9.9999", "-9.9999", "NULL"),
    }

    def test_numeric_matches(self):
        for column_type, values in self.VALUES.items():
            with self.subTest(column_type):
                name = "n" + column_type[8:].replace(", ", "_").rstrip(")")
                ms = self.table(db.MsSQL, column_type, *values, name=f"{name}_ms")
                pg = self.table(db.PostgreSQL, column_type, *values, name=f"{name}_pg")
                self.assert_matches((db.MsSQL, ms), (db.PostgreSQL, pg))

    def test_numeric_at_a_lower_scale_rounds(self):
        ms = self.table(db.MsSQL, "NUMERIC(38, 4)", "12.3456", "-0.9999", "0.0001", "-0.5000")
        pg = self.table(db.PostgreSQL, "NUMERIC(38, 2)", "12.35", "-1.00", "0.00", "-0.50")
        self.assert_matches((db.MsSQL, ms), (db.PostgreSQL, pg))

    def test_real_numeric_difference_is_reported(self):
        ms = self.table(db.MsSQL, "NUMERIC(19, 4)", "1131.2500")
        pg = self.table(db.PostgreSQL, "NUMERIC(19, 4)", "1131.2501")
        self.assert_both_directions((db.MsSQL, ms), (db.PostgreSQL, pg), count=2)

    def test_real_difference_at_a_lower_scale_is_reported(self):
        # 12.3449 rounds to 12.34, not 12.35
        ms = self.table(db.MsSQL, "NUMERIC(38, 4)", "12.3449", "-0.5000")
        pg = self.table(db.PostgreSQL, "NUMERIC(38, 2)", "12.35", "-0.50")
        self.assert_both_directions((db.MsSQL, ms), (db.PostgreSQL, pg), count=2)

    def test_numeric_key(self):
        names = []
        for cls, key_type in ((db.MsSQL, "NUMERIC(18, 0)"), (db.PostgreSQL, "BIGINT")):
            c = get_conn(cls)
            q = c.dialect.quote
            name = f"nkey_{cls.__name__.lower()}{self.suffix}"
            c.query(f"CREATE TABLE {q(name)} ({q('id')} {key_type} NOT NULL PRIMARY KEY, {q('v')} INT)", None)
            self.created.append((c, q(name)))
            for k in range(1, 41):
                # The keys reach past 2^32, so that the key ranges aren't small integers only
                c.query(f"INSERT INTO {q(name)} VALUES ({k * 300000007}, {k % 7})", None)
            if cls is db.PostgreSQL:
                c.query(f"UPDATE {q(name)} SET {q('v')} = 99 WHERE {q('id')} = {17 * 300000007}", None)
            c.query(commit)
            names.append((cls, name))
        key = str(17 * 300000007)
        for a, b in (names, names[::-1]):
            with self.subTest(f"{a[0].__name__} -> {b[0].__name__}"):
                rows = self.diff(a, b)
                self.assertEqual([("+", key), ("-", key)], sorted((sign, row[0]) for sign, row in rows))
                self.assertEqual({"99", str(17 % 7)}, {row[1] for _, row in rows})


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

    def test_leaf_tells_null_from_null_text(self):
        # Both compare small segments by row hash, which is the same for NULL and '<null>'
        pg_table = self.table(db.PostgreSQL, "VARCHAR(10)", "NULL", "'b'", "'<null>'")
        db2_table = self.table(db.Db2, "VARCHAR(10)", "'<null>'", "'c'", "'<null>'")
        self.assert_both_directions((db.PostgreSQL, pg_table), (db.Db2, db2_table), count=4)

    def test_leaf_with_nulls_matches(self):
        pg_table = self.table(db.PostgreSQL, "VARCHAR(10)", "NULL", "'b'", "'<null>'")
        db2_table = self.table(db.Db2, "VARCHAR(10)", "NULL", "'b'", "'<null>'")
        self.assert_both_directions((db.PostgreSQL, pg_table), (db.Db2, db2_table))

    def test_clob_within_the_hash_limit(self):
        text = "'" + "a" * 2000 + "'"
        pg = self.table(db.PostgreSQL, "TEXT", text)
        db2 = self.table(db.Db2, "CLOB(100K)", text)
        self.assert_both_directions((db.PostgreSQL, pg), (db.Db2, db2))

    def text_table(self, db_cls, rows, name, column_type="VARCHAR(20)") -> str:
        """Creates a table of (id, c1, c2, c3) text columns, and returns its name."""
        conn = get_conn(db_cls)
        q = conn.dialect.quote
        table_name = f"{name}{self.suffix}"
        columns = ", ".join(f"{q(c)} {column_type}" for c in ("c1", "c2", "c3"))
        conn.query(f"CREATE TABLE {q(table_name)} ({q('id')} INT NOT NULL PRIMARY KEY, {columns})", None)
        self.created.append((conn, q(table_name)))

        def literal(v):
            return "NULL" if v is None else str(v) if isinstance(v, int) else "'" + v.replace("'", "''") + "'"

        values = ", ".join("(" + ", ".join(literal(v) for v in row) + ")" for row in rows)
        conn.query(f"INSERT INTO {q(table_name)} VALUES {values}", None)
        conn.query(commit)
        return table_name

    def test_leaf_text_with_separators(self):
        # The root segment is the leaf. Every equal row must hash the same on both databases, or it
        # would be fetched by key too, and only the moved '|' and the ordinary change are reported.
        texts = ["a|b", "|", "||", "", "a  ", "é", "漢", "😀", "<null>", None]
        rows = [(i, texts[i % 10], texts[(i + 3) % 10], texts[(i * 7) % 10]) for i in range(1, 31)]
        pg = self.text_table(db.PostgreSQL, rows + [(31, "a|b", "c", "x"), (32, "v32", "w", "x")], "leaf_pg")
        db2 = self.text_table(db.Db2, rows + [(31, "a", "b|c", "x"), (32, "changed", "w", "x")], "leaf_db2")
        for a, b in (((db.PostgreSQL, pg), (db.Db2, db2)), ((db.Db2, db2), (db.PostgreSQL, pg))):
            with self.subTest(f"{a[0].__name__} -> {b[0].__name__}"):
                segments = [
                    connect_to_table(CONN_STRINGS[cls], name, ("id",), extra_columns=("c1", "c2", "c3"), thread_count=2)
                    for cls, name in (a, b)
                ]
                differ = HashDiffer(bisection_factor=2, bisection_threshold=4000)
                try:
                    diff = list(differ.diff_tables(*segments))
                finally:
                    for segment in segments:
                        segment.database.close()
                self.assertEqual(4, len(diff), diff)
                self.assertEqual(
                    {("-", "31"), ("+", "31"), ("-", "32"), ("+", "32")}, {(sign, row[0]) for sign, row in diff}
                )
                self.assertEqual(2, differ.stats["rows_fetched_by_key"])

    def test_leaf_vargraphic_uuids(self):
        # Sampling types these columns as text UUIDs, which normalize to TRIM(c). On a Db2 VARGRAPHIC
        # that stays graphic, whose OCTET_LENGTH counts UTF-16 bytes, so the length prefix must be
        # taken of the VARCHAR that's hashed, or every equal row would be fetched by key.
        rows = [(i, str(uuid.UUID(int=i * 7919)), str(uuid.UUID(int=i * 104729)), None) for i in range(1, 81)]
        changed = (80, rows[-1][1], str(uuid.UUID(int=1)), None)
        pg = self.text_table(db.PostgreSQL, rows, "leaf_uuid_pg", "VARCHAR(36)")
        db2 = self.text_table(db.Db2, rows[:-1] + [changed], "leaf_uuid_db2", "VARGRAPHIC(36)")
        for a, b in (((db.PostgreSQL, pg), (db.Db2, db2)), ((db.Db2, db2), (db.PostgreSQL, pg))):
            with self.subTest(f"{a[0].__name__} -> {b[0].__name__}"):
                segments = [
                    connect_to_table(CONN_STRINGS[cls], name, ("id",), extra_columns=("c1", "c2", "c3"), thread_count=2)
                    for cls, name in (a, b)
                ]
                differ = HashDiffer(bisection_factor=2, bisection_threshold=4000)
                try:
                    diff = list(differ.diff_tables(*segments))
                finally:
                    for segment in segments:
                        segment.database.close()
                self.assertEqual({("-", "80"), ("+", "80")}, {(sign, row[0]) for sign, row in diff})
                self.assertEqual(1, differ.stats["rows_fetched_by_key"])


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

    def test_long_clob(self):
        # HASH takes at most 32,672 bytes, so a CLOB is compared by a digest of its chunks
        long_text = "CLOB(REPEAT('a', 32000)) || CLOB(REPEAT('a', 8000))"
        changed = "CLOB(REPEAT('a', 32000)) || CLOB(REPEAT('a', 7999)) || 'b'"
        a = self.table(db.Db2, "CLOB(100K)", long_text, long_text, name="value_a")
        b = self.table(db.Db2, "CLOB(100K)", long_text, changed, name="value_b")
        for algorithm in (Algorithm.HASHDIFF, Algorithm.ROWHASH):
            diff = self.diff((db.Db2, a), (db.Db2, b), algorithm=algorithm)
            self.assertEqual(["2", "2"], [row[0] for _, row in diff])


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

    def test_mssql_numeric_is_a_decimal(self):
        info = RawColumnInfo(column_name="v", data_type="numeric", numeric_precision=19, numeric_scale=4)
        self.assertEqual(Decimal(precision=4), MsSQLDialect().parse_type(("t",), info))

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


def set_large_value(db_cls, table_name, row_id, value):
    """Stores a str or bytes of any length in column v. ASE takes at most 16384 bytes in a
    statement, so there the value is sent as a parameter."""
    conn = get_conn(db_cls)
    name = conn.dialect.quote(table_name)
    if db_cls is db.Sybase:
        sql = f"UPDATE {name} SET v = ? WHERE id = {row_id}"
        conn._run_on_connection(lambda raw: raw.cursor().execute(sql, value))
    else:
        if db_cls is db.Db2:
            # A Db2 literal holds at most 32,672 bytes, so the value is built from pieces.
            # The names are quoted, because Db2 stores them in the case they were created with.
            v, row = conn.dialect.quote("v"), f"{conn.dialect.quote('id')} = {row_id}"
            empty = "EMPTY_CLOB()" if isinstance(value, str) else "EMPTY_BLOB()"
            conn.query(f"UPDATE {name} SET {v} = {empty} WHERE {row}", None)
            for start in range(0, len(value), 8000):
                piece = value[start : start + 8000]
                literal = pg(piece) if isinstance(piece, str) else f"BX'{piece.hex()}'"
                conn.query(f"UPDATE {name} SET {v} = {v} || {literal} WHERE {row}", None)
            conn.query(commit)
            return
        if db_cls is db.MsSQL:
            literal = ms(value) if isinstance(value, str) else f"0x{value.hex()}"
        else:
            literal = pg(value) if isinstance(value, str) else f"decode('{value.hex()}', 'hex')"
        conn.query(f"UPDATE {name} SET v = {literal} WHERE id = {row_id}", None)
        conn.query(commit)


def pattern(length: int, step: int = 7) -> str:
    """Text that differs from chunk to chunk"""
    line = "".join(chr(97 + (k * step) % 26) for k in range(1009))
    return (line * (length // len(line) + 1))[:length]


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


@requires(db.Sybase, db.PostgreSQL)
class TestSybaseLargeValuesAgainstPostgreSQL(TypeTestCase):
    """text, unitext and image hold up to 2 GB, and ASE can't hash more than 16384 bytes at once,
    so both sides compare them by ``lob_digest``, which is computed in chunks."""

    ASE_SERVER_LIMIT = 1025 * 4096
    SOURCE = db.Sybase
    TARGET = db.PostgreSQL

    def tables(self, source_type, target_type, values, target_values=None):
        """A table on each server, with the values in column v. Returns (class, name) pairs."""
        pairs = []
        sides = (
            (self.SOURCE, source_type, values, "value_a"),
            (self.TARGET, target_type, target_values or values, "value_b"),
        )
        for db_cls, column_type, side, table_name in sides:
            name = self.table(db_cls, column_type, *["NULL"] * len(side), name=table_name)
            for row_id, value in enumerate(side, 1):
                if value is not None:
                    set_large_value(db_cls, name, row_id, value)
            pairs.append((db_cls, name))
        return pairs

    def assert_equal_with_both_algorithms(self, a, b):
        self.assert_matches(a, b)
        self.assertEqual([], self.diff(a, b, algorithm=Algorithm.ROWHASH))
        self.assertEqual([], self.diff(b, a, algorithm=Algorithm.ROWHASH))

    def assert_differ_with_both_algorithms(self, a, b, ids):
        expected = sorted(ids * 2)
        for x, y in ((a, b), (b, a)):
            for algorithm in (Algorithm.HASHDIFF, Algorithm.ROWHASH):
                diff = self.diff(x, y, algorithm=algorithm)
                self.assertEqual(expected, sorted(row[0] for _, row in diff), f"{algorithm} {x[0].__name__}")

    def test_text_of_every_chunk_count(self):
        lengths = [1, 4095, 4096, 4097, 8192, 16384, 16385, 100_000, 1_000_000]
        values = [pattern(n) for n in lengths] + [None]
        a, b = self.tables("TEXT", "TEXT", values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_the_digest_is_the_one_python_computes(self):
        values = [pattern(n) for n in (1, 4096, 4097, 50_000)] + ["é" * 5000 + pattern(5000)]
        a, b = self.tables("TEXT", "TEXT", values)
        expected = [(str(i), lob_digest(v)) for i, v in enumerate(values, 1)]
        segments = [
            connect_to_table(CONN_STRINGS[db_cls], name, ("id",), extra_columns=("v",), thread_count=2).with_schema()
            for db_cls, name in (a, b)
        ]
        try:
            HashDiffer()._validate_and_adjust_columns(*segments)
            for segment in segments:
                rows = sorted(segment.get_values(), key=lambda row: int(row[0]))
                self.assertEqual(expected, [tuple(row) for row in rows], segment.database.name)
        finally:
            for segment in segments:
                segment.database.close()

    def test_text_outside_ascii(self):
        values = ["é" * 5000 + "ñ" * 5000, CP850_HIGH * 200, "x" * 4095 + "é" + "y" * 4096]
        a, b = self.tables("TEXT", "TEXT", values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_spaces_at_the_end_of_a_chunk(self):
        values = [("x" * 4090 + " " * 6) * 12, " " * 5000, "x" * 4096 + " " * 4096, "abc   "]
        a, b = self.tables("TEXT", "TEXT", values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_differences_in_long_text_are_reported(self):
        text = pattern(100_000)
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
        swapped = "".join([chunks[1], chunks[0]] + chunks[2:])
        values = [text, text, text, text + " ", text, None]
        changed = [text[:-1] + "!", "!" + text[1:], swapped, text, text[:-1], ""]
        a, b = self.tables("TEXT", "TEXT", values, changed)
        self.assert_differ_with_both_algorithms(a, b, ["1", "2", "3", "4", "5", "6"])

    def test_image_against_bytea(self):
        lengths = [1, 8191, 8192, 8193, 16384, 100_000, 1_000_000]
        values = [pattern(n).encode() for n in lengths]
        # Zero bytes at the end of a chunk, and of the value
        values += [(b"q" * 8190 + b"\x00\x00") * 6, bytes(range(256)) * 100, None]
        a, b = self.tables("IMAGE", "BYTEA", values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_differences_in_image_are_reported(self):
        data = pattern(100_000).encode()
        values = [data, data, data, None]
        changed = [data[:-1] + b"!", b"!" + data[1:], data + b"\x00", b"x"]
        a, b = self.tables("IMAGE", "BYTEA", values, changed)
        self.assert_differ_with_both_algorithms(a, b, ["1", "2", "3", "4"])

    def test_varbinary_against_bytea(self):
        values = ["0x00ab", "0xff", "0x" + "c3a9" * 100, "NULL"]
        a = self.table(db.Sybase, "VARBINARY(300)", *values)
        b = self.table(db.PostgreSQL, "BYTEA", "'\\x00ab'", "'\\xff'", "'\\x" + "c3a9" * 100 + "'", "NULL")
        self.assert_equal_with_both_algorithms((db.Sybase, a), (db.PostgreSQL, b))

    def test_differences_in_varbinary_are_reported(self):
        a = self.table(db.Sybase, "VARBINARY(300)", "0x00ab", "0xff", "NULL")
        b = self.table(db.PostgreSQL, "BYTEA", "'\\x00ac'", "'\\xff00ff'", "'\\x00'")
        self.assert_differ_with_both_algorithms((db.Sybase, a), (db.PostgreSQL, b), ["1", "2", "3"])

    def test_text_over_the_server_limit_needs_rowhash(self):
        text = pattern(self.ASE_SERVER_LIMIT + 1)
        a, b = self.tables("TEXT", "TEXT", [text, "short"])
        with self.assertRaisesRegex(QueryError, "rowhash"):
            self.diff(a, b)
        self.assertEqual([], self.diff(a, b, algorithm=Algorithm.ROWHASH))
        self.assertEqual([], self.diff(b, a, algorithm=Algorithm.ROWHASH))

        set_large_value(db.PostgreSQL, b[1], 1, text[:-1] + "!")
        diff = self.diff(a, b, algorithm=Algorithm.ROWHASH)
        self.assertEqual(["1", "1"], [row[0] for _, row in diff])
        # The rows carry the digests, not the values
        self.assertEqual({lob_digest(text), lob_digest(text[:-1] + "!")}, {row[1] for _, row in diff})

    def test_a_large_value_against_null_is_reported(self):
        # The server leaves the value to the client, and its digest is NULL until then
        text = pattern(self.ASE_SERVER_LIMIT + 1)
        a, b = self.tables("TEXT", "TEXT", [text], [None])
        self.assertEqual(2, len(self.diff(a, b, algorithm=Algorithm.ROWHASH)))

    def test_unitext_in_cp850(self):
        values = ["abc ", "é" * 3000, pattern(4096), " "]
        a = self.table(db.Sybase, "UNITEXT", *map(ase, values))
        b = self.table(db.PostgreSQL, "TEXT", *map(pg, values))
        self.assert_equal_with_both_algorithms((db.Sybase, a), (db.PostgreSQL, b))

    def test_unitext_that_only_the_client_can_hash(self):
        # Over one chunk, or outside cp850 and over the limit of the conversion to UTF-8
        a = self.table(
            db.Sybase, "UNITEXT", "replicate(U&'\\65E5', 5000)", "replicate(U&'\\65E5', 200)", ase("日本語"), "NULL"
        )
        b = self.table(db.PostgreSQL, "TEXT", "repeat('日', 5000)", "repeat('日', 200)", pg("日本語"), "NULL")
        a, b = (db.Sybase, a), (db.PostgreSQL, b)
        with self.assertRaisesRegex(QueryError, "rowhash|Unicode text over"):
            self.diff(a, b)
        self.assertEqual([], self.diff(a, b, algorithm=Algorithm.ROWHASH))
        self.assertEqual([], self.diff(b, a, algorithm=Algorithm.ROWHASH))

        conn = get_conn(db.PostgreSQL)
        conn.query(f"UPDATE {conn.dialect.quote(b[1])} SET v = repeat('日', 4999) || '本' WHERE id = 1", None)
        conn.query(commit)
        diff = self.diff(a, b, algorithm=Algorithm.ROWHASH)
        self.assertEqual(["1", "1"], [row[0] for _, row in diff])

    def test_long_text_against_itself(self):
        names = []
        for name, second in (("value_a", pattern(50_000, step=11)), ("value_b", pattern(50_000))):
            names.append(self.table(db.Sybase, "TEXT", "NULL", "NULL", "NULL", name=name))
            set_large_value(db.Sybase, names[-1], 1, pattern(50_000))
            set_large_value(db.Sybase, names[-1], 2, second)
        a, b = [(db.Sybase, name) for name in names]
        self.assertEqual(2, len(self.diff(a, b)))
        self.assertEqual(2, len(self.diff(a, b, algorithm=Algorithm.ROWHASH)))


class LargeValueCases:
    """Large text and bytes between two databases, of which neither needs the client."""

    SOURCE = None
    TARGET = None
    TEXT = {db.Sybase: "TEXT", db.PostgreSQL: "TEXT", db.MsSQL: "NVARCHAR(MAX)", db.Db2: "CLOB(10M)"}
    BYTES = {db.Sybase: "IMAGE", db.PostgreSQL: "BYTEA", db.MsSQL: "VARBINARY(MAX)", db.Db2: "BLOB(10M)"}

    tables = TestSybaseLargeValuesAgainstPostgreSQL.tables
    assert_equal_with_both_algorithms = TestSybaseLargeValuesAgainstPostgreSQL.assert_equal_with_both_algorithms
    assert_differ_with_both_algorithms = TestSybaseLargeValuesAgainstPostgreSQL.assert_differ_with_both_algorithms

    def test_text(self):
        lengths = [1, 4095, 4096, 4097, 16385, 100_000, 1_000_000]
        values = [pattern(n) for n in lengths] + ["é" * 5000 + "ñ" * 5000, ("x" * 4090 + " " * 6) * 12, None]
        a, b = self.tables(self.TEXT[self.SOURCE], self.TEXT[self.TARGET], values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_text_differences(self):
        text = pattern(100_000)
        values = [text, text, text + " ", None]
        changed = [text[:-1] + "!", "!" + text[1:], text, "x"]
        a, b = self.tables(self.TEXT[self.SOURCE], self.TEXT[self.TARGET], values, changed)
        self.assert_differ_with_both_algorithms(a, b, ["1", "2", "3", "4"])

    def test_bytes(self):
        lengths = [1, 8191, 8192, 8193, 100_000, 1_000_000]
        values = [pattern(n).encode() for n in lengths] + [
            (b"q" * 8190 + b"\x00\x00") * 6,
            bytes(range(256)) * 100,
            None,
        ]
        a, b = self.tables(self.BYTES[self.SOURCE], self.BYTES[self.TARGET], values)
        self.assert_equal_with_both_algorithms(a, b)

    def test_bytes_differences(self):
        data = pattern(100_000).encode()
        a, b = self.tables(
            self.BYTES[self.SOURCE],
            self.BYTES[self.TARGET],
            [data, data, None],
            [data[:-1] + b"!", data + b"\x00", b"x"],
        )
        self.assert_differ_with_both_algorithms(a, b, ["1", "2", "3"])


@requires(db.MsSQL, db.PostgreSQL)
class TestLargeValuesMsSQLAgainstPostgreSQL(LargeValueCases, TypeTestCase):
    SOURCE = db.MsSQL
    TARGET = db.PostgreSQL

    def test_text_outside_the_basic_multilingual_plane(self):
        # NVARCHAR counts a surrogate pair as two, and the other databases count one character
        values = ["\U0001f600" * 5000 + "end", "日本" * 9000, "x" * 4095 + "\U0001f600" + "y" * 5000]
        a, b = self.tables("NVARCHAR(MAX)", "TEXT", values)
        self.assert_equal_with_both_algorithms(a, b)


@requires(db.MsSQL, db.PostgreSQL)
class TestMsSQLBoundedTextAgainstPostgreSQL(TypeTestCase):
    """SQL Server hashes text declared up to 4000 long as NVARCHAR(n), without MAX types, when the
    declared lengths prove the UTF-8 input fits, and as before otherwise. The checksums must equal
    PostgreSQL's either way."""

    def checksum_sql(self, ms_table, pg_table) -> str:
        segments = [
            connect_to_table(CONN_STRINGS[cls], name, ("id",), extra_columns=("v",), thread_count=2)
            for cls, name in ((db.MsSQL, ms_table), (db.PostgreSQL, pg_table))
        ]
        try:
            segments = [segment.with_schema() for segment in segments]
            HashDiffer()._validate_and_adjust_columns(*segments)
            ms_segment = segments[0]
            query = ms_segment.make_select().select(
                Count(), Checksum([NormalizeAsString(this[c]) for c in ("id", "v")])
            )
            return ms_segment.database.compile(query)
        finally:
            for segment in segments:
                segment.database.close()

    def test_nvarchar(self):
        values = ["é", "漢", "\U0001f600", "ab  ", "", None, "<null>", "a|b", "mix é日\U0001f600z"]
        a = self.table(db.MsSQL, "NVARCHAR(20)", *map(ms, values))
        b = self.table(db.PostgreSQL, "TEXT", *map(pg, values))
        sql = self.checksum_sql(a, b)
        self.assertIn("CONVERT(NVARCHAR(20), [v])", sql)
        self.assertNotIn("MAX", sql)
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b))

    def test_varchar(self):
        values = ["é", "ab  ", "", None, "x", "it's"]
        a = self.table(db.MsSQL, "VARCHAR(10)", *map(pg, values))
        b = self.table(db.PostgreSQL, "TEXT", *map(pg, values))
        self.assertIn("CONVERT(NVARCHAR(10), [v])", self.checksum_sql(a, b))
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b))

    def test_column_shorter_than_null_text(self):
        # coalesce() must not cut '<null>' to the column's length
        a = self.table(db.MsSQL, "VARCHAR(1)", "NULL", "'a'", "NULL")
        b = self.table(db.PostgreSQL, "TEXT", "NULL", "'a'", "NULL")
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b))

    def test_alphanumeric_text_keeps_its_length(self):
        # Sampling turns this column into an alphanumeric type, which must keep the length
        a = self.table(db.MsSQL, "VARCHAR(12)", "'abc123'", "'xyz789'")
        b = self.table(db.PostgreSQL, "VARCHAR(12)", "'abc123'", "'xyz789'")
        self.assertIn("CONVERT(NVARCHAR(12), [v])", self.checksum_sql(a, b))
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b))

    def test_real_text_differences_are_reported(self):
        a = self.table(db.MsSQL, "NVARCHAR(20)", *map(ms, ["漢a", "\U0001f600", "ab ", None, ""]))
        b = self.table(db.PostgreSQL, "TEXT", *map(pg, ["漢b", "\U0001f601", "ab", "", " "]))
        self.assert_both_directions((db.MsSQL, a), (db.PostgreSQL, b), count=10)

    def test_text_near_the_bound(self):
        # 2600 x 3 UTF-8 bytes, plus the key: inside VARCHAR(7998), so it's hashed without MAX types
        a = self.table(db.MsSQL, "NVARCHAR(2600)", "REPLICATE(N'漢', 2600)", "REPLICATE(N'漢', 2599) + N'字'")
        b = self.table(db.PostgreSQL, "TEXT", "repeat('漢', 2600)", "repeat('漢', 2600)")
        sql = self.checksum_sql(a, b)
        self.assertIn("CONVERT(NVARCHAR(2651)", sql)  # 50 for the key, the separator, and 2600
        self.assertNotIn("MAX", sql)
        diff = self.diff((db.MsSQL, a), (db.PostgreSQL, b))
        self.assertEqual({"2"}, {row[0] for _, row in diff})
        self.assertEqual(2, len(diff))
        b_same = self.table(db.PostgreSQL, "TEXT", "repeat('漢', 2600)", "repeat('漢', 2599) || '字'", name="same")
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b_same))

    def test_longer_declared_text_takes_the_max_path(self):
        # 2667 x 漢 is 8001 UTF-8 bytes, which the bounded form would cut without an error
        a = self.table(db.MsSQL, "NVARCHAR(3000)", "REPLICATE(N'漢', 2667)", "REPLICATE(N'漢', 2666) + N'字'")
        b = self.table(db.PostgreSQL, "TEXT", "repeat('漢', 2667)", "repeat('漢', 2667)")
        sql = self.checksum_sql(a, b)
        self.assertIn("CONVERT(VARCHAR(MAX), CONVERT(NVARCHAR(MAX)", sql)
        self.assertIn("CONVERT(NVARCHAR(MAX), coalesce(", sql)
        diff = self.diff((db.MsSQL, a), (db.PostgreSQL, b))
        self.assertEqual({"2"}, {row[0] for _, row in diff})
        self.assertEqual(2, len(diff))

    def test_non_text_is_hashed_without_conversion(self):
        values = "9999999999999999999999999999999999.9999", "-0.5", "0", "NULL"
        a = self.table(db.MsSQL, "DECIMAL(38, 4)", *values)
        b = self.table(db.PostgreSQL, "DECIMAL(38, 4)", *values)
        sql = self.checksum_sql(a, b)
        self.assertNotIn("COLLATE", sql)
        self.assertNotIn("NVARCHAR", sql)
        self.assert_matches((db.MsSQL, a), (db.PostgreSQL, b))

    def test_real_non_text_difference_is_reported(self):
        a = self.table(db.MsSQL, "DECIMAL(38, 4)", "-0.5", "NULL")
        b = self.table(db.PostgreSQL, "DECIMAL(38, 4)", "-0.4", "0")
        self.assert_both_directions((db.MsSQL, a), (db.PostgreSQL, b), count=4)


@requires(db.Db2, db.PostgreSQL)
class TestLargeValuesDb2AgainstPostgreSQL(LargeValueCases, TypeTestCase):
    SOURCE = db.Db2
    TARGET = db.PostgreSQL


@requires(db.Sybase, db.Db2)
class TestLargeValuesSybaseAgainstDb2(LargeValueCases, TypeTestCase):
    SOURCE = db.Sybase
    TARGET = db.Db2


@requires(db.MsSQL, db.Db2)
class TestLargeValuesMsSQLAgainstDb2(LargeValueCases, TypeTestCase):
    SOURCE = db.MsSQL
    TARGET = db.Db2


@requires(db.Sybase, db.MsSQL)
class TestLargeValuesSybaseAgainstMsSQL(LargeValueCases, TypeTestCase):
    SOURCE = db.Sybase
    TARGET = db.MsSQL


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

    def test_numeric(self):
        # ASE types numeric(38,0) as Integer
        for column_type, values in (
            ("NUMERIC(38, 0)", ("0", "-1", "12", "9" * 38, "-" + "9" * 38, "NULL")),
            (
                "NUMERIC(19, 4)",
                ("0", "0.3200", "-0.5000", "-0.0001", "-0.9999", "1131.2500", "9" * 15 + ".9999", "NULL"),
            ),
        ):
            with self.subTest(column_type):
                name = "n" + column_type[8:].replace(", ", "_").rstrip(")")
                a = self.table(db.Sybase, column_type, *values, name=f"{name}_ase")
                b = self.table(db.MsSQL, column_type, *values, name=f"{name}_ms")
                self.assert_matches((db.Sybase, a), (db.MsSQL, b))

    def test_real_numeric_difference_is_reported(self):
        a = self.table(db.Sybase, "NUMERIC(19, 4)", "1131.2500", "-0.5000")
        b = self.table(db.MsSQL, "NUMERIC(19, 4)", "1131.2501", "-0.5000")
        self.assert_both_directions((db.Sybase, a), (db.MsSQL, b), count=2)

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

    def test_text_over_the_expression_limit(self):
        conn = get_conn(db.Sybase)
        a = self.table(db.Sybase, "TEXT", "'x'", name="value_a")
        b = self.table(db.Sybase, "TEXT", "'x'", name="value_b")
        # Expressions stop at 16384 bytes, so the long value comes from the client
        conn.query(f"UPDATE {conn.dialect.quote(a)} SET v = '{'y' * 10000}'", None)
        set_large_value(db.Sybase, b, 1, "y" * 20000)
        self.assertEqual(20000, conn.query(f"SELECT datalength(v) FROM {conn.dialect.quote(b)}", int))
        self.assertEqual(2, len(self.diff((db.Sybase, a), (db.Sybase, b))))
        set_large_value(db.Sybase, a, 1, "y" * 20000)
        self.assertEqual([], self.diff((db.Sybase, a), (db.Sybase, b)))
        self.assertEqual([], self.diff((db.Sybase, a), (db.Sybase, b), algorithm=Algorithm.ROWHASH))

    def test_joindiff_still_explains_the_limit_on_text(self):
        # joindiff compares the values themselves, in one expression
        a = self.table(db.Sybase, "TEXT", "'x'", name="value_a")
        b = self.table(db.Sybase, "TEXT", "'x'", name="value_b")
        set_large_value(db.Sybase, b, 1, "y" * 20000)
        with self.assertRaisesRegex(QueryError, "text over 16384 bytes"):
            self.diff((db.Sybase, a), (db.Sybase, b), algorithm=Algorithm.JOINDIFF)

    def test_rows_of_long_text_columns(self):
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
            # Each text column is hashed by itself, so together they aren't limited to an expression
            diff = list(diff_tables(*segments, bisection_factor=2, bisection_threshold=4, algorithm=Algorithm.HASHDIFF))
            self.assertEqual([], diff)
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
