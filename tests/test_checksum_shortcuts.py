"""Covers the shortcuts that make Db2 and ASE checksums cheaper: Db2 reads the MD5 tail with TRANSLATE,
renders each column once, skips ROUND at the native scale and renders DATE with CHAR(x, ISO), and
ASE skips the numeric cast at the native scale. Each type gets a test where the values match, and
one where a real difference must still be reported.
"""
import hashlib
from itertools import count
import unittest

from data_diff import databases as db
from data_diff.databases.base import CHECKSUM_OFFSET
from tests.common import get_conn, str_to_checksum
from tests.test_cross_db_types import TypeTestCase, ase, pg, requires


def _tail_search(accept, start=0) -> str:
    """The first string 'x<n>' whose MD5 ends in 12 hex digits that pass accept()."""
    for n in count(start):
        text = f"x{n}"
        if accept(hashlib.md5(text.encode()).hexdigest()[-12:]):
            return text


# MD5 tails with no letters, only letters, and leading zeros, which decide where the hex weights matter
DIGITS_ONLY = _tail_search(lambda t: t.isdigit())
LETTERS_ONLY = _tail_search(lambda t: all(c in "abcdef" for c in t))
LEADING_ZEROS = _tail_search(lambda t: t.startswith("0000"))
TRAILING_ZEROS = _tail_search(lambda t: t.endswith("0000"))
ALL_F_RUN = _tail_search(lambda t: t.count("f") >= 6 and t[0] == "f")


@requires(db.Db2)
class TestDb2Md5Tail(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn(db.Db2)

    def tail(self, text: str) -> int:
        literal = "'" + text.replace("'", "''") + "'"
        sql = f"SELECT {self.conn.dialect._md5_tail(literal, CHECKSUM_OFFSET)} FROM SYSIBM.SYSDUMMY1"
        return self.conn.query(sql, int)

    def test_the_tail_is_the_reference_checksum(self):
        for text in ["", "a", " ", "hello world", "é", "日本語", "a|b", "<null>", DIGITS_ONLY]:
            with self.subTest(text=text):
                self.assertEqual(str_to_checksum(text), self.tail(text))

    def test_extreme_hex_tails(self):
        for name, text in [
            ("digits only", DIGITS_ONLY),
            ("letters only", LETTERS_ONLY),
            ("leading zeros", LEADING_ZEROS),
            ("trailing zeros", TRAILING_ZEROS),
            ("many f", ALL_F_RUN),
        ]:
            with self.subTest(name=name, tail=hashlib.md5(text.encode()).hexdigest()[-12:]):
                self.assertEqual(str_to_checksum(text), self.tail(text))

    def test_a_different_string_gives_a_different_tail(self):
        self.assertNotEqual(self.tail("a"), self.tail("b"))
        self.assertNotEqual(self.tail(""), self.tail(" "))


@requires(db.Db2, db.PostgreSQL)
class TestDb2DecimalScale(TypeTestCase):
    def test_native_scale_matches(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "-0.0001", "0.0001", "NULL"
        a = self.table(db.Db2, "DECIMAL(19, 4)", *values)
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", *values)
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_largest_precision_matches(self):
        values = "99999999999999999999.9999999999", "-99999999999999999999.9999999999", "0.0000000001", "NULL"
        a = self.table(db.Db2, "DECIMAL(30, 10)", *values)
        b = self.table(db.PostgreSQL, "NUMERIC(30, 10)", *values)
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_lower_scale_on_the_other_side_still_rounds(self):
        a = self.table(db.Db2, "DECIMAL(10, 3)", "1.125", "-1.125", "2.004", "0.005", "-0.005")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 2)", "1.13", "-1.13", "2.00", "0.01", "-0.01")
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_lower_scale_difference_is_reported(self):
        a = self.table(db.Db2, "DECIMAL(10, 3)", "1.125")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 2)", "1.12")
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)

    def test_native_scale_difference_is_reported(self):
        a = self.table(db.Db2, "DECIMAL(19, 4)", "1131.2500", "-0.0001")
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", "1131.2501", "-0.0001")
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)

    def test_scale_zero(self):
        a = self.table(db.Db2, "DECIMAL(10, 0)", "0", "-1", "9999999999", "NULL")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 0)", "0", "-1", "9999999999", "NULL")
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))


@requires(db.Sybase, db.PostgreSQL)
class TestSybaseMoneyAndNumeric(TypeTestCase):
    def test_numeric_at_native_scale_matches(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "-0.0001", "NULL"
        a = self.table(db.Sybase, "NUMERIC(19, 4)", *values)
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_money_matches_numeric(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "NULL"
        a = self.table(db.Sybase, "MONEY", *values)
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", *values)
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_money_difference_in_the_fourth_decimal_is_reported(self):
        a = self.table(db.Sybase, "MONEY", "1131.2500")
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", "1131.2501")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_numeric_difference_in_the_last_decimal_is_reported(self):
        a = self.table(db.Sybase, "NUMERIC(19, 4)", "1131.2500", "-0.0001")
        b = self.table(db.PostgreSQL, "NUMERIC(19, 4)", "1131.2500", "-0.0002")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)

    def test_numeric_at_a_lower_scale_still_rounds(self):
        a = self.table(db.Sybase, "NUMERIC(10, 3)", "1.125", "-1.125", "2.004")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 2)", "1.13", "-1.13", "2.00")
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))

    def test_numeric_at_a_lower_scale_difference_is_reported(self):
        a = self.table(db.Sybase, "NUMERIC(10, 3)", "1.125")
        b = self.table(db.PostgreSQL, "NUMERIC(10, 2)", "1.12")
        self.assert_both_directions((db.Sybase, a), (db.PostgreSQL, b), count=2)


@requires(db.Sybase, db.Db2)
class TestSybaseMoneyAgainstDb2(TypeTestCase):
    def test_money_matches_decimal(self):
        values = "0.3200", "-0.5000", "1131.2500", "-1234567.8900", "0", "NULL"
        a = self.table(db.Sybase, "MONEY", *values)
        b = self.table(db.Db2, "DECIMAL(19, 4)", *values)
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_money_difference_is_reported(self):
        a = self.table(db.Sybase, "MONEY", "1131.2500")
        b = self.table(db.Db2, "DECIMAL(19, 4)", "1131.2501")
        self.assert_both_directions((db.Sybase, a), (db.Db2, b), count=2)


DATES = "'0001-01-01'", "'9999-12-31'", "'2024-02-29'", "'1970-01-01'", "'1999-12-31'", "NULL"


@requires(db.Db2, db.PostgreSQL)
class TestDb2Date(TypeTestCase):
    def test_extremes_and_null_match(self):
        a = self.table(db.Db2, "DATE", *DATES)
        b = self.table(db.PostgreSQL, "DATE", *DATES)
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_a_day_off_is_reported(self):
        a = self.table(db.Db2, "DATE", "'9999-12-31'")
        b = self.table(db.PostgreSQL, "DATE", "'9999-12-30'")
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)

    def test_null_against_a_date_is_reported(self):
        a = self.table(db.Db2, "DATE", "NULL")
        b = self.table(db.PostgreSQL, "DATE", "'0001-01-01'")
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)


@requires(db.Sybase, db.Db2)
class TestDateAcrossAseAndDb2(TypeTestCase):
    def test_extremes_and_null_match(self):
        a = self.table(db.Sybase, "DATE", *DATES)
        b = self.table(db.Db2, "DATE", *DATES)
        self.assert_matches((db.Sybase, a), (db.Db2, b))

    def test_a_day_off_is_reported(self):
        a = self.table(db.Sybase, "DATE", "'9999-12-31'")
        b = self.table(db.Db2, "DATE", "'9999-12-30'")
        self.assert_both_directions((db.Sybase, a), (db.Db2, b), count=2)


@requires(db.Db2, db.PostgreSQL)
class TestDb2Vargraphic(TypeTestCase):
    TEXT = "plain", "café", "日本語", "a|b", "trailing  ", "", "NULL"

    def literals(self):
        return [v if v == "NULL" else pg(v) for v in self.TEXT]

    def test_vargraphic_matches_varchar(self):
        # A Db2 VARGRAPHIC holds UTF-16, so the checksum must convert it to UTF-8 before HASH
        a = self.table(db.Db2, "VARGRAPHIC(50)", *self.literals())
        b = self.table(db.PostgreSQL, "VARCHAR(50)", *self.literals())
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_vargraphic_difference_is_reported(self):
        a = self.table(db.Db2, "VARGRAPHIC(50)", pg("日本語"))
        b = self.table(db.PostgreSQL, "VARCHAR(50)", pg("日本誤"))
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)

    def test_vargraphic_trailing_space_difference_is_reported(self):
        a = self.table(db.Db2, "VARGRAPHIC(50)", pg("a "))
        b = self.table(db.PostgreSQL, "VARCHAR(50)", pg("a"))
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=2)


@requires(db.Db2, db.PostgreSQL)
class TestNullNextToNullText(TypeTestCase):
    """Checksums have always treated NULL and the text '<null>' as equal, and the single coalesce
    in Db2's row hash must keep that, and must keep NULL apart from the empty string."""

    def checksum_of(self, db_cls, column_type, value):
        literal = value if value == "NULL" else pg(value)
        self.calls = getattr(self, "calls", 0) + 1
        return self.table(db_cls, column_type, literal, name=f"nn{self.calls}_{db_cls.__name__.lower()}")

    def test_null_and_the_null_text_hash_like_postgresql(self):
        for value in ("NULL", "<null>"):
            a = self.checksum_of(db.Db2, "VARCHAR(20)", value)
            b = self.checksum_of(db.PostgreSQL, "VARCHAR(20)", value)
            first, second = self.checksums((db.Db2, a), (db.PostgreSQL, b))
            self.assertEqual(first, second, value)

    def test_null_and_the_null_text_are_equal_in_a_checksum(self):
        a = self.checksum_of(db.Db2, "VARCHAR(20)", "NULL")
        b = self.checksum_of(db.PostgreSQL, "VARCHAR(20)", "<null>")
        first, second = self.checksums((db.Db2, a), (db.PostgreSQL, b))
        self.assertEqual(first, second)

    def test_null_and_the_empty_string_differ(self):
        a = self.checksum_of(db.Db2, "VARCHAR(20)", "NULL")
        b = self.checksum_of(db.PostgreSQL, "VARCHAR(20)", "")
        first, second = self.checksums((db.Db2, a), (db.PostgreSQL, b))
        self.assertNotEqual(first, second)

    def test_a_null_value_next_to_the_null_text_in_other_rows(self):
        a = self.table(db.Db2, "VARCHAR(20)", "NULL", "'<null>'", "'x'", "NULL")
        b = self.table(db.PostgreSQL, "VARCHAR(20)", "NULL", "'<null>'", "'x'", "NULL")
        self.assert_matches((db.Db2, a), (db.PostgreSQL, b))

    def test_swapping_a_value_and_its_neighbour_is_reported(self):
        a = self.table(db.Db2, "VARCHAR(20)", "'x'", "'y'")
        b = self.table(db.PostgreSQL, "VARCHAR(20)", "'y'", "'x'")
        self.assert_both_directions((db.Db2, a), (db.PostgreSQL, b), count=4)


@requires(db.Sybase, db.PostgreSQL)
class TestSybaseNullText(TypeTestCase):
    def test_null_text_and_values_match(self):
        a = self.table(db.Sybase, "VARCHAR(20)", "NULL", ase("<null>"), ase("x"))
        b = self.table(db.PostgreSQL, "VARCHAR(20)", "NULL", pg("<null>"), pg("x"))
        self.assert_matches((db.Sybase, a), (db.PostgreSQL, b))


if __name__ == "__main__":
    unittest.main()
