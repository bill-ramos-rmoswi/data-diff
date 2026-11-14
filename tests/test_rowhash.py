"""RowHashDiffer reads each table once, and must report what HashDiffer reports."""
import hashlib
from typing import Callable
import unittest
from unittest import mock

from data_diff import databases as db
from data_diff.hashdiff_tables import HashDiffer
from data_diff.queries.api import commit
from data_diff.rowhash_tables import RowHashDiffer
from data_diff.table_segment import TableSegment
from data_diff.utils import BINARY_DIGEST_CHUNK, TEXT_DIGEST_CHUNK, lob_digest
from tests.common import DiffTestCase, table_segment, test_each_database_in_list

TEST_DATABASES = {db.MySQL, db.PostgreSQL, db.MsSQL, db.Db2, db.Sybase, db.DuckDB}

test_each_database: Callable = test_each_database_in_list(TEST_DATABASES)

COLUMNS = ["id", "userid", "rating", "comment"]


class TestLobDigest(unittest.TestCase):
    def test_null_and_empty(self):
        self.assertIsNone(lob_digest(None))
        self.assertEqual("0", lob_digest(""))
        self.assertEqual("0", lob_digest(b""))

    def test_one_chunk_is_the_md5_of_its_index_and_text(self):
        self.assertEqual(str(int(hashlib.md5(b"0|abc").hexdigest()[-12:], 16)), lob_digest("abc"))
        self.assertEqual(lob_digest("abc"), lob_digest(b"abc"))

    def test_text_is_cut_by_character_and_bytes_by_byte(self):
        # 3 bytes for each character, so a chunk of characters is not a chunk of bytes
        text = "日" * (TEXT_DIGEST_CHUNK + 1)
        self.assertNotEqual(lob_digest(text), lob_digest(text.encode("utf-8")))
        ascii_text = "a" * (BINARY_DIGEST_CHUNK + 1)
        self.assertNotEqual(lob_digest(ascii_text), lob_digest(ascii_text.encode()))

    def test_the_order_of_chunks_matters(self):
        a, b = "a" * TEXT_DIGEST_CHUNK, "b" * TEXT_DIGEST_CHUNK
        self.assertNotEqual(lob_digest(a + b), lob_digest(b + a))

    def test_bytes_like_values(self):
        self.assertEqual(lob_digest(b"abc"), lob_digest(memoryview(b"abc")))
        self.assertEqual(lob_digest(b"abc"), lob_digest(bytearray(b"abc")))


@test_each_database
class TestRowHashDiffer(DiffTestCase):
    src_schema = {"id": int, "userid": int, "rating": float, "comment": str}
    dst_schema = {"id": int, "userid": int, "rating": float, "comment": str}

    def setUp(self):
        super().setUp()
        self.table = table_segment(
            self.connection,
            self.table_src_path,
            "id",
            extra_columns=("userid", "rating", "comment"),
            case_sensitive=False,
        )
        self.table2 = table_segment(
            self.connection,
            self.table_dst_path,
            "id",
            extra_columns=("userid", "rating", "comment"),
            case_sensitive=False,
        )

    def fill(self, src_rows, dst_rows):
        queries = []
        if src_rows:
            queries.append(self.src_table.insert_rows(src_rows, columns=COLUMNS))
        if dst_rows:
            queries.append(self.dst_table.insert_rows(dst_rows, columns=COLUMNS))
        self.connection.query(queries + [commit])

    def both(self, **options):
        """The diff from RowHashDiffer, after checking that HashDiffer reports the same."""
        expected = sorted(HashDiffer(bisection_factor=3, bisection_threshold=4).diff_tables(self.table, self.table2))
        actual = sorted(RowHashDiffer(**options).diff_tables(self.table, self.table2))
        self.assertEqual(expected, actual)
        return actual

    def test_equal_tables(self):
        rows = [[i, i * 10, i / 4, f"comment {i}"] for i in range(1, 21)]
        self.fill(rows, rows)
        self.assertEqual([], self.both())

    def test_changed_missing_and_extra_rows(self):
        rows = [[i, i * 10, i / 4, f"comment {i}"] for i in range(1, 41)]
        changed = [list(row) for row in rows if row[0] not in (3, 17)]
        changed[10][3] = "changed"
        changed[20][1] = -1
        changed.append([100, 1, 1.5, None])
        self.fill(rows, changed)
        diff = self.both()
        self.assertEqual({"3", "17", "100", str(changed[10][0]), str(changed[20][0])}, {row[0] for _, row in diff})
        self.assertEqual(7, len(diff))

    def test_many_segments(self):
        rows = [[i, i, 0.5, f"row {i}"] for i in range(1, 400, 3)]
        changed = [list(row) for row in rows]
        changed[0][3] = "first"
        changed[-1][3] = "last"
        changed[60][3] = "middle"
        self.fill(rows, changed)
        diff = self.both(segment_rows=10)
        self.assertEqual(6, len(diff))

    def test_each_table_is_read_once(self):
        rows = [[i, i, 0.5, f"row {i}"] for i in range(1, 101)]
        changed = [list(row) for row in rows]
        changed[4][3] = "changed"
        self.fill(rows, changed)

        hashed, fetched = [], []
        get_key_hashes, get_rows_by_keys = TableSegment.get_key_hashes, TableSegment.get_rows_by_keys

        def count_hashes(segment):
            rows = get_key_hashes(segment)
            hashed.append(len(rows))
            return rows

        def count_fetches(segment, keys):
            fetched.append(list(keys))
            return get_rows_by_keys(segment, keys)

        with mock.patch.object(TableSegment, "get_key_hashes", count_hashes), mock.patch.object(
            TableSegment, "get_rows_by_keys", count_fetches
        ), mock.patch.object(TableSegment, "count_and_checksum", side_effect=AssertionError("checksummed")):
            diff = list(RowHashDiffer(segment_rows=25).diff_tables(self.table, self.table2))

        self.assertEqual(2, len(diff))
        # Every row was hashed once on each side, and only the changed row was read again
        self.assertEqual(200, sum(hashed))
        self.assertEqual([[("5",)], [("5",)]], fetched)

    def test_rowhash_differ_sql_is_unchanged(self):
        # Only a HashDiffer leaf hashes text with its length in front. RowHashDiffer keeps the row
        # hash that checksums sum, and with it the blind spot for '|' that moves between columns.
        rows = [[i, i, 0.5, f"row|{i}"] for i in range(1, 11)]
        changed = [list(row) for row in rows]
        changed[2][3] = "changed"
        self.fill(rows, changed)

        calls, statements = [], []
        get_key_hashes, query = TableSegment.get_key_hashes, type(self.connection)._query

        def spy_hashes(segment, *args, **kwargs):
            calls.append(kwargs)
            return get_key_hashes(segment, *args, **kwargs)

        def spy_query(database, sql_code):
            if isinstance(sql_code, str):
                statements.append(sql_code)
            return query(database, sql_code)

        with mock.patch.object(TableSegment, "get_key_hashes", spy_hashes), mock.patch.object(
            type(self.connection), "_query", spy_query
        ):
            diff = list(RowHashDiffer().diff_tables(self.table, self.table2))
        self.assertEqual({("-", "3"), ("+", "3")}, {(sign, row[0]) for sign, row in diff})
        self.assertEqual([{}, {}], calls)
        self.assertTrue(statements)
        self.assertFalse([s for s in statements if "octet_length" in s.lower()])

    def test_recheck_reports_what_still_differs(self):
        rows = [[i, i, 0.5, f"row {i}"] for i in range(1, 11)]
        changed = [list(row) for row in rows]
        changed[2][3] = "changed"
        changed[7][3] = "changed"
        self.fill(rows, changed)

        differ = RowHashDiffer(recheck_delay=0.01)
        diff_rows = differ._diff_rows
        calls = []

        def catch_up_after_first_read(table1, table2, keys):
            result = diff_rows(table1, table2, keys)
            if not calls:
                # The copy of row 3 arrives between the two reads
                quote = self.connection.dialect.quote
                self.connection.query(
                    f"UPDATE {quote(self.table_dst_name)} SET {quote('comment')} = 'row 3' WHERE {quote('id')} = 3",
                    None,
                )
                self.connection.query(commit)
            calls.append(list(keys))
            return result

        with mock.patch.object(RowHashDiffer, "_diff_rows", side_effect=catch_up_after_first_read):
            diff = list(differ.diff_tables(self.table, self.table2))
        self.assertEqual({"8"}, {row[0] for _, row in diff})
        self.assertEqual([[("3",), ("8",)], [("3",), ("8",)]], calls)


@test_each_database
class TestRowHashCompoundKey(DiffTestCase):
    src_schema = {"id": int, "userid": int, "comment": str}
    dst_schema = {"id": int, "userid": int, "comment": str}

    def test_compound_key(self):
        columns = ["id", "userid", "comment"]
        rows = [[i, j, f"{i}-{j}"] for i in range(1, 8) for j in range(1, 6)]
        changed = [list(row) for row in rows if (row[0], row[1]) != (2, 2)]
        changed[12][2] = "changed"
        self.connection.query(
            [
                self.src_table.insert_rows(rows, columns=columns),
                self.dst_table.insert_rows(changed, columns=columns),
                commit,
            ]
        )
        a = table_segment(
            self.connection, self.table_src_path, ("id", "userid"), extra_columns=("comment",), case_sensitive=False
        )
        b = table_segment(
            self.connection, self.table_dst_path, ("id", "userid"), extra_columns=("comment",), case_sensitive=False
        )
        expected = sorted(HashDiffer(bisection_factor=3, bisection_threshold=4).diff_tables(a, b))
        actual = sorted(RowHashDiffer(segment_rows=5, key_batch=1).diff_tables(a, b))
        self.assertEqual(expected, actual)
        self.assertEqual(3, len(actual))
