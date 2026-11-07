"""Diffs a fact table across PostgreSQL, SQL Server and Db2, through the public Python API.

The table has the shape of AdventureWorksDW's FactProductInventory: a compound key, a date and a
money column. Text adds non-ASCII values, and costs of 1,000 or more cover thousands separators.
"""
from itertools import permutations
import unittest

from parameterized import parameterized

from data_diff import connect_to_table, diff_tables
from data_diff import databases as db
from data_diff.queries.api import commit
from tests.common import CONN_STRINGS, get_conn, random_table_suffix

COST_AND_NOTE_TYPES = {
    db.PostgreSQL: ("NUMERIC(19, 4)", "VARCHAR(50)"),
    db.MsSQL: ("MONEY", "NVARCHAR(50)"),
    db.Db2: ("DECIMAL(19, 4)", "VARCHAR(200)"),
}

KEY_COLUMNS = ("product_key", "date_key")
EXTRA_COLUMNS = ("movement_date", "unit_cost", "note")

ROWS = [
    (1, 20101228, "2010-12-28", "0.3200", "plain"),
    (1, 20101229, "2010-12-29", "0.0000", "zero"),
    (1, 20130102, "2013-01-02", "-0.5000", "under one"),
    (2, 20101228, "2010-12-28", "1131.2500", "thousands"),
    (2, 20140630, "2014-06-30", "2171.2942", "café"),
    (3, 20101228, "2010-12-28", "-1234567.8900", "日本語"),
    (3, 20110214, "2011-02-14", "12.3400", "emoji \U0001f600"),
    (4, 20101228, "2010-12-28", "99.9900", None),
] + [(5, 20110000 + i, f"2011-02-{i:02}", f"{i}.5000", f"filler {i}") for i in range(1, 25)]


def _quoted(conn, *names) -> str:
    return ", ".join(conn.dialect.quote(name) for name in names)


def _literal(conn, value) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        return str(value)
    return f"N'{value}'" if isinstance(conn, db.MsSQL) else f"'{value}'"


def _row_sql(conn, row) -> str:
    product_key, date_key, movement_date, unit_cost, note = row
    values = [str(product_key), str(date_key), f"'{movement_date}'", unit_cost, _literal(conn, note)]
    return f"({', '.join(values)})"


def _as_diff_row(row) -> tuple:
    return tuple(None if value is None else str(value) for value in row)


TARGET_DATABASES = [cls for cls in COST_AND_NOTE_TYPES if cls in CONN_STRINGS]
DIRECTIONS = [
    (f"{source.__name__}_to_{target.__name__}", source, target) for source, target in permutations(TARGET_DATABASES, 2)
]


class TestCrossDatabaseFactTable(unittest.TestCase):
    def setUp(self):
        self.table_name = f"fact{random_table_suffix()}"
        self.tables = []

    def tearDown(self):
        for conn, table_sql in self.tables:
            conn.query(f"DROP TABLE {table_sql}", None)
            conn.query(commit)

    def _create_table(self, db_cls, rows):
        conn = get_conn(db_cls)
        cost_type, note_type = COST_AND_NOTE_TYPES[db_cls]
        table_sql = conn.dialect.quote(self.table_name)
        q = conn.dialect.quote
        conn.query(
            f"CREATE TABLE {table_sql} ("
            f"{q('product_key')} INT NOT NULL, {q('date_key')} INT NOT NULL, {q('movement_date')} DATE NOT NULL, "
            f"{q('unit_cost')} {cost_type} NOT NULL, {q('note')} {note_type}, "
            f"PRIMARY KEY ({_quoted(conn, *KEY_COLUMNS)}))",
            None,
        )
        self.tables.append((conn, table_sql))
        self._insert(conn, rows)
        return conn

    def _insert(self, conn, rows):
        table_sql = conn.dialect.quote(self.table_name)
        columns = _quoted(conn, *KEY_COLUMNS, *EXTRA_COLUMNS)
        for row in rows:
            conn.query(f"INSERT INTO {table_sql} ({columns}) VALUES {_row_sql(conn, row)}", None)
        conn.query(commit)

    def _diff(self, source, target):
        tables = [
            connect_to_table(
                CONN_STRINGS[db_cls], self.table_name, KEY_COLUMNS, thread_count=2, extra_columns=EXTRA_COLUMNS
            )
            for db_cls in (source, target)
        ]
        # A low threshold makes the diff go through checksums, not only row downloads
        diff = sorted(diff_tables(*tables, bisection_factor=2, bisection_threshold=4), key=str)

        # An open PostgreSQL connection stays in a transaction, which blocks DROP TABLE in tearDown
        for segment in tables:
            segment.database.close()
        return diff

    @parameterized.expand(DIRECTIONS, skip_on_empty=True)
    def test_identical_tables(self, _, source, target):
        self._create_table(source, ROWS)
        self._create_table(target, ROWS)
        self.assertEqual([], self._diff(source, target))

    @parameterized.expand(DIRECTIONS, skip_on_empty=True)
    def test_reports_exact_differences(self, _, source, target):
        self._create_table(source, ROWS)

        changed = (2, 20140630, "2014-06-30", "2171.2943", "café")
        changed_note = (3, 20101228, "2010-12-28", "-1234567.8900", "日本誟")
        missing = ROWS[0]
        extra = (9, 20200101, "2020-01-01", "5.0000", "only in target")

        target_rows = [row for row in ROWS if row[:2] not in (missing[:2], changed[:2], changed_note[:2])]
        self._create_table(target, target_rows + [changed, changed_note, extra])

        expected = sorted(
            [
                ("-", _as_diff_row(missing)),
                ("-", _as_diff_row(ROWS[4])),
                ("+", _as_diff_row(changed)),
                ("-", _as_diff_row(ROWS[5])),
                ("+", _as_diff_row(changed_note)),
                ("+", _as_diff_row(extra)),
            ],
            key=str,
        )
        self.assertEqual(expected, self._diff(source, target))
