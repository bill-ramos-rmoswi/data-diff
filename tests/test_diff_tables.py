from datetime import datetime, timedelta
from typing import Callable
import uuid
import unittest
import unittest.mock

import attrs

from data_diff.queries.api import table, this, commit, code
from data_diff.utils import ArithAlphanumeric, numberToAlphanum

from data_diff.hashdiff_tables import HashDiffer
from data_diff.joindiff_tables import JoinDiffer
from data_diff.table_segment import TableSegment, split_space, Vector
from data_diff import databases as db

from tests.common import str_to_checksum, test_each_database_in_list, DiffTestCase, table_segment


TEST_DATABASES = {
    db.MySQL,
    db.PostgreSQL,
    db.Oracle,
    db.Redshift,
    db.Snowflake,
    db.BigQuery,
    db.Presto,
    db.Trino,
    db.Vertica,
    db.MsSQL,
    db.Db2,
    db.Sybase,
}

test_each_database: Callable = test_each_database_in_list(TEST_DATABASES)


class TestUtils(unittest.TestCase):
    def test_split_space(self):
        for i in range(0, 10):
            for j in range(1, 16328, 17):
                for n in range(1, 32):
                    r = split_space(i, j + i + n, n)
                    assert len(r) == n, f"split_space({i}, {j+n}, {n}) = {(r)}"


@test_each_database
class TestDates(DiffTestCase):
    src_schema = {"id": int, "datetime": datetime, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table
        self.now = now = datetime.now()

        rows = [
            (now - timedelta(days=50), "50 days ago"),
            (now - timedelta(hours=3), "3 hours ago"),
            (now - timedelta(minutes=10), "10 mins ago"),
            (now - timedelta(seconds=1), "1 second ago"),
            (now, "now"),
        ]

        self.connection.query(
            [
                src_table.insert_rows((i, ts, s) for i, (ts, s) in enumerate(rows)),
                table(self.table_dst_path).create(src_table),
                commit,
                src_table.insert_row(len(rows), self.now - timedelta(seconds=5), "5 seconds ago"),
                commit,
            ]
        )

    def test_init(self):
        a = table_segment(
            self.connection, self.table_src_path, "id", "datetime", max_update=self.now, case_sensitive=False
        )
        self.assertRaises(ValueError, table_segment, self.connection, self.table_src_path, "id", max_update=self.now)

    def test_basic(self):
        differ = HashDiffer(bisection_factor=10, bisection_threshold=100)
        a = table_segment(self.connection, self.table_src_path, "id", "datetime", case_sensitive=False)
        b = table_segment(self.connection, self.table_dst_path, "id", "datetime", case_sensitive=False)
        assert a.count() == 6
        assert b.count() == 5

        assert not list(differ.diff_tables(a, a))
        self.assertEqual(len(list(differ.diff_tables(a, b))), 1)

    def test_offset(self):
        differ = HashDiffer(bisection_factor=2, bisection_threshold=10)
        sec1 = self.now - timedelta(seconds=3)
        a = table_segment(self.connection, self.table_src_path, "id", "datetime", max_update=sec1, case_sensitive=False)
        b = table_segment(self.connection, self.table_dst_path, "id", "datetime", max_update=sec1, case_sensitive=False)
        assert a.count() == 4, a.count()
        assert b.count() == 3

        assert not list(differ.diff_tables(a, a))
        self.assertEqual(len(list(differ.diff_tables(a, b))), 1)

        a = table_segment(self.connection, self.table_src_path, "id", "datetime", min_update=sec1, case_sensitive=False)
        b = table_segment(self.connection, self.table_dst_path, "id", "datetime", min_update=sec1, case_sensitive=False)
        assert a.count() == 2
        assert b.count() == 2
        assert not list(differ.diff_tables(a, b))

        day1 = self.now - timedelta(days=1)

        a = table_segment(
            self.connection,
            self.table_src_path,
            "id",
            "datetime",
            min_update=day1,
            max_update=sec1,
            case_sensitive=False,
        )
        b = table_segment(
            self.connection,
            self.table_dst_path,
            "id",
            "datetime",
            min_update=day1,
            max_update=sec1,
            case_sensitive=False,
        )
        assert a.count() == 3
        assert b.count() == 2
        assert not list(differ.diff_tables(a, a))
        self.assertEqual(len(list(differ.diff_tables(a, b))), 1)


@test_each_database
class TestDiffTables(DiffTestCase):
    src_schema = {"id": int, "userid": int, "movieid": int, "rating": float, "timestamp": datetime}
    dst_schema = {"id": int, "userid": int, "movieid": int, "rating": float, "timestamp": datetime}

    def setUp(self):
        super().setUp()

        self.table = table_segment(self.connection, self.table_src_path, "id", "timestamp", case_sensitive=False)
        self.table2 = table_segment(self.connection, self.table_dst_path, "id", "timestamp", case_sensitive=False)

        self.differ = HashDiffer(bisection_factor=3, bisection_threshold=4)

    def test_properties_on_empty_table(self):
        table = self.table.with_schema()
        self.assertEqual(0, table.count())
        self.assertEqual(None, table.count_and_checksum()[1])

    def test_get_values(self):
        time = "2022-01-01 00:00:00.000000"
        time_obj = datetime.fromisoformat(time)

        cols = "id userid movieid rating timestamp".split()
        id_ = self.connection.query(
            [self.src_table.insert_row(1, 1, 1, 9, time_obj, columns=cols), commit, self.src_table.select(this.id)], int
        )

        table = self.table.with_schema()

        self.assertEqual(1, table.count())
        concatted = str(id_) + "|" + time
        self.assertEqual(str_to_checksum(concatted), table.count_and_checksum()[1])

    def test_diff_small_tables(self):
        time = "2022-01-01 00:00:00"
        time_obj = datetime.fromisoformat(time)

        cols = "id userid movieid rating timestamp".split()
        self.connection.query(
            [
                self.src_table.insert_rows([[1, 1, 1, 9, time_obj], [2, 2, 2, 9, time_obj]], columns=cols),
                self.dst_table.insert_rows([[1, 1, 1, 9, time_obj]], columns=cols),
                commit,
            ]
        )

        diff_res = self.differ.diff_tables(self.table, self.table2)
        info = diff_res.info_tree.info
        diff = list(diff_res)

        expected = [("-", ("2", time + ".000000"))]
        self.assertEqual(expected, diff)
        self.assertEqual(2, info.rowcounts[1])
        self.assertEqual(1, info.rowcounts[2])

    def test_non_threaded(self):
        differ = HashDiffer(bisection_factor=3, bisection_threshold=4, threaded=False)

        time = "2022-01-01 00:00:00"
        time_obj = datetime.fromisoformat(time)
        cols = "id userid movieid rating timestamp".split()
        self.connection.query(
            [
                self.src_table.insert_row(1, 1, 1, 9, time_obj, columns=cols),
                self.dst_table.insert_row(1, 1, 1, 9, time_obj, columns=cols),
                commit,
            ]
        )

        diff = list(differ.diff_tables(self.table, self.table2))
        self.assertEqual(diff, [])

    def test_diff_table_above_bisection_threshold(self):
        time = "2022-01-01 00:00:00"
        time_obj = datetime.fromisoformat(time)

        cols = "id userid movieid rating timestamp".split()

        self.connection.query(
            [
                self.src_table.insert_rows(
                    [
                        [1, 1, 1, 9, time_obj],
                        [2, 2, 2, 9, time_obj],
                        [3, 3, 3, 9, time_obj],
                        [4, 4, 4, 9, time_obj],
                        [5, 5, 5, 9, time_obj],
                    ],
                    columns=cols,
                ),
                self.dst_table.insert_rows(
                    [
                        [1, 1, 1, 9, time_obj],
                        [2, 2, 2, 9, time_obj],
                        [3, 3, 3, 9, time_obj],
                        [4, 4, 4, 9, time_obj],
                    ],
                    columns=cols,
                ),
                commit,
            ]
        )

        diff_res = self.differ.diff_tables(self.table, self.table2)
        info = diff_res.info_tree.info
        diff = list(diff_res)

        expected = [("-", ("5", time + ".000000"))]
        self.assertEqual(expected, diff)
        self.assertEqual(5, info.rowcounts[1])
        self.assertEqual(4, info.rowcounts[2])

    def test_return_empty_array_when_same(self):
        time = "2022-01-01 00:00:00"
        time_obj = datetime.fromisoformat(time)

        cols = "id userid movieid rating timestamp".split()

        self.connection.query(
            [
                self.src_table.insert_row(1, 1, 1, 9, time_obj, columns=cols),
                self.dst_table.insert_row(1, 1, 1, 9, time_obj, columns=cols),
                commit,
            ]
        )

        diff = list(self.differ.diff_tables(self.table, self.table2))
        self.assertEqual([], diff)

    def test_diff_sorted_by_key(self):
        time = "2022-01-01 00:00:00"
        time2 = "2021-01-01 00:00:00"

        time_obj = datetime.fromisoformat(time)
        time_obj2 = datetime.fromisoformat(time2)

        cols = "id userid movieid rating timestamp".split()

        self.connection.query(
            [
                self.src_table.insert_rows(
                    [
                        [1, 1, 1, 9, time_obj],
                        [2, 2, 2, 9, time_obj2],
                        [3, 3, 3, 9, time_obj],
                        [4, 4, 4, 9, time_obj2],
                        [5, 5, 5, 9, time_obj],
                    ],
                    columns=cols,
                ),
                self.dst_table.insert_rows(
                    [
                        [1, 1, 1, 9, time_obj],
                        [2, 2, 2, 9, time_obj],
                        [3, 3, 3, 9, time_obj],
                        [4, 4, 4, 9, time_obj],
                        [5, 5, 5, 9, time_obj],
                    ],
                    columns=cols,
                ),
                commit,
            ]
        )

        differ = HashDiffer(bisection_factor=2)
        diff = set(differ.diff_tables(self.table, self.table2))
        expected = {
            ("-", ("2", time2 + ".000000")),
            ("+", ("2", time + ".000000")),
            ("-", ("4", time2 + ".000000")),
            ("+", ("4", time + ".000000")),
        }
        self.assertEqual(expected, diff)


@test_each_database
class TestDiffTables2(DiffTestCase):
    src_schema = {"id": int, "rating": float, "timestamp": datetime}
    dst_schema = {"id2": int, "rating2": float, "timestamp2": datetime}

    def test_diff_column_names(self):
        time = "2022-01-01 00:00:00"
        time2 = "2021-01-01 00:00:00"

        time_obj = datetime.fromisoformat(time)
        time_obj2 = datetime.fromisoformat(time2)

        self.connection.query(
            [
                self.src_table.insert_rows(
                    [
                        [1, 9, time_obj],
                        [2, 9, time_obj2],
                        [3, 9, time_obj],
                        [4, 9, time_obj2],
                        [5, 9, time_obj],
                    ],
                    columns=["id", "rating", "timestamp"],
                ),
                self.dst_table.insert_rows(
                    [
                        [1, 9, time_obj],
                        [2, 9, time_obj2],
                        [3, 9, time_obj],
                        [4, 9, time_obj2],
                        [5, 9, time_obj],
                    ],
                    columns=["id2", "rating2", "timestamp2"],
                ),
            ]
        )

        table1 = table_segment(self.connection, self.table_src_path, "id", "timestamp", case_sensitive=False)
        table2 = table_segment(self.connection, self.table_dst_path, "id2", "timestamp2", case_sensitive=False)

        differ = HashDiffer(bisection_factor=2)
        diff = list(differ.diff_tables(table1, table2))
        assert diff == []


@test_each_database
class TestUUIDs(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table

        self.new_uuid = uuid.uuid1(32132131)

        self.connection.query(
            [
                src_table.insert_rows((uuid.uuid1(i), str(i)) for i in range(100)),
                table(self.table_dst_path).create(src_table),
                src_table.insert_row(self.new_uuid, "This one is different"),
                commit,
            ]
        )

        self.a = table_segment(
            self.connection, self.table_src_path, "id", extra_columns=("text_comment",), case_sensitive=False
        ).with_schema()
        self.b = table_segment(
            self.connection, self.table_dst_path, "id", extra_columns=("text_comment",), case_sensitive=False
        ).with_schema()

    def test_string_keys(self):
        differ = HashDiffer(bisection_factor=2)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, [("-", (str(self.new_uuid), "This one is different"))])

        # At this point the tables should already have a schema, to ensure the column is detected as UUID and not alpanum
        self.connection.query(self.src_table.insert_row("unexpected", "<-- this bad value should not break us"))
        self.assertRaises(ValueError, list, differ.diff_tables(self.a, self.b))

    def test_where_sampling(self):
        a = attrs.evolve(self.a, where="1=1")

        differ = HashDiffer(bisection_factor=2)
        diff = list(differ.diff_tables(a, self.b))
        self.assertEqual(diff, [("-", (str(self.new_uuid), "This one is different"))])

        a_empty = attrs.evolve(self.a, where="1=0")
        self.assertRaises(ValueError, list, differ.diff_tables(a_empty, self.b))


@test_each_database_in_list(TEST_DATABASES - {db.MySQL})
class TestAlphanumericKeys(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table
        self.new_alphanum = "aBcDeFgHiz"

        values = []
        for i in range(0, 10000, 1000):
            a = ArithAlphanumeric(numberToAlphanum(i), max_len=10)
            if not a and isinstance(self.connection, (db.Oracle, db.Sybase)):
                # Skip empty string, because Oracle treats it as NULL, and ASE stores it as a space
                continue

            values.append((str(a), str(i)))

        queries = [
            src_table.insert_rows(values),
            table(self.table_dst_path).create(src_table),
            src_table.insert_row(self.new_alphanum, "This one is different"),
            commit,
        ]

        for query in queries:
            self.connection.query(query, None)

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

    def test_alphanum_keys(self):
        differ = HashDiffer(bisection_factor=2, bisection_threshold=3)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, [("-", (str(self.new_alphanum), "This one is different"))])

        self.connection.query([self.src_table.insert_row("@@@", "<-- this bad value should not break us"), commit])

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

        self.assertRaises(NotImplementedError, list, differ.diff_tables(self.a, self.b))


@test_each_database_in_list(TEST_DATABASES - {db.MySQL})
class TestVaryingAlphanumericKeys(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table

        values = []
        for i in range(0, 10000, 1000):
            a = ArithAlphanumeric(numberToAlphanum(i * i))
            if not a and isinstance(self.connection, (db.Oracle, db.Sybase)):
                # Skip empty string, because Oracle treats it as NULL, and ASE stores it as a space
                continue

            values.append((str(a), str(i)))

        self.new_alphanum = "aBcDeFgHiJ"

        queries = [
            src_table.insert_rows(values),
            table(self.table_dst_path).create(src_table),
            src_table.insert_row(self.new_alphanum, "This one is different"),
            commit,
        ]

        self.connection.query(queries)

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

    def test_varying_alphanum_keys(self):
        # Test the class itself
        values = ["---", "0123", "Z9", "ZZ", "_", "a", "a-", "a123", "a_"]
        alphanums = [ArithAlphanumeric(v) for v in values]
        alphanums.sort()
        self.assertEqual(values, [str(i) for i in alphanums])

        for a in alphanums:
            assert a - a == 0

        differ = HashDiffer(bisection_factor=2)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, [("-", (str(self.new_alphanum), "This one is different"))])

        self.connection.query(
            self.src_table.insert_row("@@@", "<-- this bad value should not break us"),
            commit,
        )

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

        self.assertRaises(NotImplementedError, list, differ.diff_tables(self.a, self.b))


@test_each_database
class TestTableSegment(DiffTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.table = table_segment(self.connection, self.table_src_path, "id", "timestamp", case_sensitive=False)
        self.table2 = table_segment(self.connection, self.table_dst_path, "id", "timestamp", case_sensitive=False)

    def test_table_segment(self):
        early = datetime(2021, 1, 1, 0, 0)
        late = datetime(2022, 1, 1, 0, 0)
        self.assertRaises(ValueError, attrs.evolve, self.table, min_update=late, max_update=early)

        self.assertRaises(ValueError, attrs.evolve, self.table, min_key=Vector((10,)), max_key=Vector((0,)))

    def test_case_awareness(self):
        src_table = table(self.table_src_path, schema={"id": int, "userid": int, "timestamp": datetime})

        cols = "id userid timestamp".split()
        time = "2022-01-01 00:00:00.000000"
        time_obj = datetime.fromisoformat(time)

        self.connection.query(
            [src_table.create(), src_table.insert_rows([[1, 9, time_obj], [2, 2, time_obj]], columns=cols), commit]
        )

        res = tuple(attrs.evolve(self.table, key_columns=("Id",), case_sensitive=False).with_schema().query_key_range())
        self.assertEqual(res, (("1",), ("2",)))

        self.assertRaises(
            KeyError, attrs.evolve(self.table, key_columns=("Id",), case_sensitive=True).with_schema().query_key_range
        )


@test_each_database
class TestTableUUID(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table

        values = []
        for i in range(10):
            uuid_value = uuid.uuid1(i)
            values.append((uuid_value, uuid_value))

        self.null_uuid = uuid.uuid1(32132131)

        self.connection.query(
            [
                src_table.insert_rows(values),
                table(self.table_dst_path).create(src_table),
                src_table.insert_row(self.null_uuid, None),
                commit,
            ]
        )

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

    def test_uuid_column_with_nulls(self):
        differ = HashDiffer(bisection_factor=2)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, [("-", (str(self.null_uuid), None))])


@test_each_database
class TestTableNullRowChecksum(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        src_table = self.src_table

        self.null_uuid = uuid.uuid1(1)
        self.connection.query(
            [
                src_table.insert_row(uuid.uuid1(1), "1"),
                table(self.table_dst_path).create(src_table),
                src_table.insert_row(self.null_uuid, None),  # Add a row where a column has NULL value
                commit,
            ]
        )

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

    def test_uuid_columns_with_nulls(self):
        """
        Here we test a case when in one segment one or more columns has only null values. For example,
        Table A:
        | id   |   value   |
        |------|-----------|
        | pk_1 | 'value_1' |
        | pk_2 |    NULL   |

        Table B:
        | id   |   value   |
        |------|-----------|
        | pk_1 | 'value_1' |

        We can choose some bisection factor and bisection threshold (2 and 3 for our example, respectively)
        that one segment will look like ('pk_2', NULL). Some databases, when we do a cast these values to string and
        try to concatenate, some databases return NULL when concatenating (for example, MySQL). As the result, all next
        operations like substring, sum etc return nulls that leads incorrect diff results: ('pk_2', null) should be in
        diff results, but it's not. This test helps to detect such cases.
        """

        differ = HashDiffer(bisection_factor=2, bisection_threshold=3)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, [("-", (str(self.null_uuid), None))])


@test_each_database
class TestConcatMultipleColumnWithNulls(DiffTestCase):
    src_schema = {"id": str, "c1": str, "c2": str}
    dst_schema = {"id": str, "c1": str, "c2": str}

    def setUp(self):
        super().setUp()

        src_values = []
        dst_values = []

        self.diffs = []
        for i in range(0, 8):
            pk = uuid.uuid1(i)
            src_row = (str(pk), str(i), None)
            dst_row = (str(pk), str(i) + "-different", None)

            src_values.append(src_row)
            dst_values.append(dst_row)

            self.diffs.append(("-", src_row))
            self.diffs.append(("+", dst_row))

        self.connection.query(
            [
                self.src_table.insert_rows(src_values),
                self.dst_table.insert_rows(dst_values),
                commit,
            ]
        )

        self.a = table_segment(
            self.connection, self.table_src_path, "id", extra_columns=("c1", "c2"), case_sensitive=False
        )
        self.b = table_segment(
            self.connection, self.table_dst_path, "id", extra_columns=("c1", "c2"), case_sensitive=False
        )

    def test_tables_are_different(self):
        """
        Here we test a case when in one segment one or more columns has only null values. For example,
        Table A:
        | id   | c1 |  c2  |
        |------|----|------|
        | pk_1 | 1  | NULL |
        | pk_2 | 2  | NULL |
                ...
        | pk_n | n | NULL |

        Table B:
        | id   |   c1   |  c2  |
        |------|--------|------|
        | pk_1 | 1-diff | NULL |
        | pk_2 | 2-diff | NULL |
                  ...
        | pk_n | n-diff | NULL |

        To calculate a checksum, we need to concatenate string values by rows. If both tables have columns with NULL
        value, it may lead that concat(pk_i, i, NULL) == concat(pk_i, i-diff, NULL). This test handle such cases.
        """

        differ = HashDiffer(bisection_factor=2, bisection_threshold=4)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, self.diffs)


@test_each_database
class TestTableTableEmpty(DiffTestCase):
    src_schema = {"id": str, "text_comment": str}
    dst_schema = {"id": str, "text_comment": str}

    def setUp(self):
        super().setUp()

        self.null_uuid = uuid.uuid1(1)

        diffs = [(uuid.uuid1(i), str(i)) for i in range(100)]
        self.connection.query([self.src_table.insert_rows(diffs), commit])

        self.a = table_segment(self.connection, self.table_src_path, "id", "text_comment", case_sensitive=False)
        self.b = table_segment(self.connection, self.table_dst_path, "id", "text_comment", case_sensitive=False)

        self.differ = HashDiffer(bisection_factor=2)

    def test_right_table_empty(self):
        # NotImplementedError: Cannot use a column of type Text(_notes=[]) as a key
        self.assertRaises(NotImplementedError, list, self.differ.diff_tables(self.a, self.b))

    def test_left_table_empty(self):
        # NotImplementedError: Cannot use a column of type Text(_notes=[]) as a key
        self.assertRaises(NotImplementedError, list, self.differ.diff_tables(self.a, self.b))


class TestInfoTree(DiffTestCase):
    db_cls = db.MySQL
    src_schema = dst_schema = dict(id=int)

    def test_info_tree_root(self):
        db = self.connection
        db.query(
            [
                self.src_table.insert_rows([i] for i in range(1000)),
                self.dst_table.insert_rows([i] for i in range(2000)),
            ]
        )

        ts1 = TableSegment(db, self.src_table.path, ("id",))
        ts2 = TableSegment(db, self.dst_table.path, ("id",))

        for differ in (HashDiffer(bisection_threshold=64), JoinDiffer(True)):
            diff_res = differ.diff_tables(ts1, ts2)
            diff = list(diff_res)
            info_tree = diff_res.info_tree
            assert info_tree.info.is_diff
            assert info_tree.info.diff_count == 1000
            self.assertEqual(info_tree.info.rowcounts, {1: 1000, 2: 2000})


class TestDuplicateTables(DiffTestCase):
    db_cls = db.MySQL

    src_schema = {"id": int, "data": str}
    dst_schema = {"id": int, "data": str}

    def setUp(self):
        """
        table 1:
            (12, 'ABCDE'),
            (12, 'ABCDE');
        table 2:
            (4,'ABCDEF'),
            (4,'ABCDE'),
            (4,'ABCDE'),
            (6,'ABCDE'),
            (6,'ABCDE'),
            (6,'ABCDE');
        """

        super().setUp()

        src_values = [(12, "ABCDE"), (12, "ABCDE")]
        dst_values = [(4, "ABCDEF"), (4, "ABCDE"), (4, "ABCDE"), (6, "ABCDE"), (6, "ABCDE"), (6, "ABCDE")]

        self.diffs = [("-", (str(r[0]), r[1])) for r in src_values] + [("+", (str(r[0]), r[1])) for r in dst_values]

        self.connection.query([self.src_table.insert_rows(src_values), self.dst_table.insert_rows(dst_values), commit])

        self.a = table_segment(
            self.connection, self.table_src_path, "id", extra_columns=("data",), case_sensitive=False
        )
        self.b = table_segment(
            self.connection, self.table_dst_path, "id", extra_columns=("data",), case_sensitive=False
        )

    def test_duplicates(self):
        """If there are duplicates in data, we want to return them as well"""

        differ = HashDiffer(bisection_factor=2, bisection_threshold=4)
        diff = list(differ.diff_tables(self.a, self.b))
        self.assertEqual(diff, self.diffs)


@test_each_database
class TestCompoundKeySimple1(DiffTestCase):
    src_schema = {"id": int, "id2": int}
    dst_schema = {"id": int, "id2": int}

    def test_simple1(self):
        N = 1000
        K = N + 1
        V1 = N + 1
        V2 = N * 1000 + 2

        diffs = [(i, i + N) for i in range(N)]
        self.connection.query(
            [
                self.src_table.insert_rows(diffs + [(K, V1)]),
                self.dst_table.insert_rows(diffs + [(K, V2)]),
                commit,
            ]
        )

        expected = {("-", (str(K), str(V1))), ("+", (str(K), str(V2)))}
        differ = HashDiffer()

        a = TableSegment(self.connection, self.src_table.path, ("id",), extra_columns=("id2",))
        b = TableSegment(self.connection, self.dst_table.path, ("id",), extra_columns=("id2",))
        diff = set(differ.diff_tables(a, b))
        self.assertEqual(diff, expected)

        aa = TableSegment(self.connection, self.src_table.path, ("id", "id2"))
        bb = TableSegment(self.connection, self.dst_table.path, ("id", "id2"))
        diff = set(differ.diff_tables(aa, bb))
        self.assertEqual(diff, expected)


@test_each_database
class TestCompoundKeySimple2(DiffTestCase):
    src_schema = {"id": int, "id2": int}
    dst_schema = {"id": int, "id2": int}

    def test_simple2(self):
        N = 1000
        K = N + 1
        V1 = N + 1
        V2 = N * 1000 + 2

        diffs = [(i + 1, i + N) for i in range(N)]  # pk=[1..1000], no dupes
        self.connection.query(
            [
                self.src_table.insert_rows(diffs + [(K, V1)]),  # exclusive pk=1001
                self.dst_table.insert_rows(diffs + [(0, V2)]),  # exclusive pk=0
                commit,
            ]
        )

        expected = {("-", (str(K), str(V1))), ("+", (str(0), str(V2)))}
        differ = HashDiffer()

        a = TableSegment(self.connection, self.src_table.path, ("id",), extra_columns=("id2",))
        b = TableSegment(self.connection, self.dst_table.path, ("id",), extra_columns=("id2",))
        diff = set(differ.diff_tables(a, b))
        self.assertEqual(diff, expected)

        aa = TableSegment(self.connection, self.src_table.path, ("id", "id2"))
        bb = TableSegment(self.connection, self.dst_table.path, ("id", "id2"))
        diff = set(differ.diff_tables(aa, bb))
        self.assertEqual(diff, expected)


@test_each_database
class TestCompoundKeySimple3(DiffTestCase):
    src_schema = {"id": int, "id2": int}
    dst_schema = {"id": int, "id2": int}

    def test_negative_keys(self):
        N = 1000
        K = -N + 1
        V1 = N + 1
        V2 = -N * 1000 + 2

        diffs = [(i, i + N) for i in range(N)]
        self.connection.query(
            [
                self.src_table.insert_rows(diffs + [(K, V1)]),
                self.dst_table.insert_rows(diffs + [(K, V2)]),
                commit,
            ]
        )

        expected = {("-", (str(K), str(V1))), ("+", (str(K), str(V2)))}
        differ = HashDiffer()

        a = TableSegment(self.connection, self.src_table.path, ("id",), extra_columns=("id2",))
        b = TableSegment(self.connection, self.dst_table.path, ("id",), extra_columns=("id2",))
        diff = set(differ.diff_tables(a, b))
        self.assertEqual(diff, expected)

        aa = TableSegment(self.connection, self.src_table.path, ("id", "id2"))
        bb = TableSegment(self.connection, self.dst_table.path, ("id", "id2"))
        diff = set(differ.diff_tables(aa, bb))
        self.assertEqual(diff, expected)


@test_each_database
class TestCompoundKeyAlphanum(DiffTestCase):
    src_schema = {"id": str, "id2": int, "comment": str}
    dst_schema = {"id": str, "id2": int, "comment": str}

    def setUp(self):
        super().setUp()

        rows = [(uuid.uuid1(i), i, str(i)) for i in range(100)]
        rows2 = list(rows)
        x = rows2[9]
        rows2[9] = (x[0], 9000, x[2])
        self.connection.query(
            [
                self.src_table.insert_rows(rows),
                self.dst_table.insert_rows(rows2),
                commit,
            ]
        )

    def test_compound_key(self):
        a = TableSegment(self.connection, self.src_table.path, ("id",), extra_columns=("id2", "comment"))
        b = TableSegment(self.connection, self.dst_table.path, ("id",), extra_columns=("id2", "comment"))

        differ = HashDiffer()
        diff = list(differ.diff_tables(a, b))
        uuid = diff[0][1][0]
        self.assertEqual(diff, [("-", (uuid, "9", "9")), ("+", (uuid, "9000", "9"))])

        aa = TableSegment(self.connection, self.src_table.path, ("id", "id2"), "comment")
        bb = TableSegment(self.connection, self.dst_table.path, ("id", "id2"), "comment")
        diff = list(differ.diff_tables(aa, bb))
        uuid = diff[0][1][0]
        self.assertEqual(diff, [("-", (uuid, "9", "9")), ("+", (uuid, "9000", "9"))])

        self.assertRaises(ValueError, list, differ.diff_tables(aa, a))


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHash(DiffTestCase):
    """PostgreSQL and Db2 compare a small segment by the key and row hash of each row, and download
    only the rows that differ. The result must be the same as comparing every row."""

    src_schema = {"id": int, "c1": str, "c2": str}
    dst_schema = {"id": int, "c1": str, "c2": str}

    def segments(self):
        return [
            table_segment(self.connection, path, "id", extra_columns=("c1", "c2"), case_sensitive=False)
            for path in (self.table_src_path, self.table_dst_path)
        ]

    def diff(self, rows1, rows2):
        self.connection.query([self.src_table.insert_rows(rows1), self.dst_table.insert_rows(rows2), commit])
        differ = HashDiffer(bisection_factor=2, bisection_threshold=100)
        diff = list(differ.diff_tables(*self.segments()))
        return diff, differ.stats

    def test_equal_tables_report_nothing(self):
        rows = [(i, f"v{i}", None if i % 3 else "<null>") for i in range(1, 21)]
        diff, stats = self.diff(rows, rows)
        self.assertEqual([], diff)
        self.assertEqual(20, stats["rows_downloaded"])
        self.assertEqual(0, stats["rows_fetched_by_key"])

    def test_null_against_null_text_is_reported(self):
        # Their row hashes are equal, and only the NULL mask tells them apart
        rows1 = [(1, "a", None), (2, "b", "x"), (3, "c", "<null>"), (4, "d", None)]
        rows2 = [(1, "a", "<null>"), (2, "b", "x"), (3, "c", None), (4, "d", None)]
        diff, stats = self.diff(rows1, rows2)
        expected = [
            ("-", ("1", "a", None)),
            ("+", ("1", "a", "<null>")),
            ("-", ("3", "c", "<null>")),
            ("+", ("3", "c", None)),
        ]
        self.assertEqual(expected, diff)
        self.assertEqual(2, stats["rows_fetched_by_key"])

    def test_differences_are_reported(self):
        rows1 = [(i, f"v{i}", "same") for i in range(1, 31)]
        rows2 = [(i, f"v{i}" if i % 7 else "changed", "same") for i in range(2, 32)]
        diff, stats = self.diff(rows1, rows2)
        changed = [i for i in range(2, 31) if i % 7 == 0]
        expected = (
            [("-", (str(i), f"v{i}", "same")) for i in [1] + changed]
            + [("+", (str(i), "changed", "same")) for i in changed]
            + [("+", ("31", "v31", "same"))]
        )
        self.assertEqual(sorted(expected), sorted(diff))
        # The changed rows, and the rows only one side has, which are in different segments
        self.assertEqual(len(changed) + 2, stats["rows_fetched_by_key"])

    def test_segment_where_most_rows_differ_downloads_every_row(self):
        # More suspects than one query fetches, and more than half the rows
        patch = unittest.mock.patch("data_diff.hashdiff_tables.LEAF_KEY_BATCH", 5)
        patch.start()
        self.addCleanup(patch.stop)
        rows1 = [(i, f"v{i}", "same") for i in range(1, 21)]
        rows2 = [(i, f"v{i}" if i % 4 == 0 else "changed", "same") for i in range(1, 21)]
        diff, stats = self.diff(rows1, rows2)
        changed = [i for i in range(1, 21) if i % 4]
        expected = [("-", (str(i), f"v{i}", "same")) for i in changed] + [
            ("+", (str(i), "changed", "same")) for i in changed
        ]
        self.assertEqual(sorted(expected), sorted(diff))
        self.assertEqual(0, stats.get("rows_fetched_by_key", 0))

    def test_duplicate_keys_are_reported(self):
        rows1 = [(1, "a", "x"), (2, "b", "x"), (2, "b", "x")]
        rows2 = [(1, "a", "x"), (2, "b", "x")]
        diff, _ = self.diff(rows1, rows2)
        self.assertEqual([("-", ("2", "b", "x")), ("-", ("2", "b", "x")), ("+", ("2", "b", "x"))], diff)


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashTextUUIDKey(DiffTestCase):
    """A text UUID key in mixed case can't be fetched by key, because the key's literal is in one
    case. The segment must then download every row, not lose the rows it missed."""

    src_schema = {"id": str, "v": int}
    dst_schema = {"id": str, "v": int}

    def test_mixed_case_keys_are_all_reported(self):
        ids = [str(uuid.UUID(int=i * 7919 + 12345)) for i in range(30)]
        ids = [u.upper() if i % 2 else u for i, u in enumerate(ids)]
        rows1 = [(u, i) for i, u in enumerate(ids)]
        rows2 = [(u, i + 1000 if i in (3, 4) else i) for i, u in enumerate(ids)]
        self.connection.query([self.src_table.insert_rows(rows1), self.dst_table.insert_rows(rows2), commit])
        segments = [
            table_segment(self.connection, path, "id", extra_columns=("v",), case_sensitive=False)
            for path in (self.table_src_path, self.table_dst_path)
        ]
        diff = list(HashDiffer(bisection_factor=2, bisection_threshold=100).diff_tables(*segments))
        expected = [("-", (ids[i], str(i))) for i in (3, 4)] + [("+", (ids[i], str(i + 1000))) for i in (3, 4)]
        self.assertEqual(sorted(expected), sorted(diff))

    def test_key_that_isnt_a_uuid_is_reported(self):
        # The sampled rows (the first 64) are UUIDs, so the key is a UUID type, and the last key
        # can't be made into one. It sorts inside the key range, so it's in a leaf.
        ids = [str(uuid.UUID(int=(i << 121) + i)) for i in range(1, 100)] + ["5zzz-not-a-uuid"]
        rows1 = [(u, i) for i, u in enumerate(ids)]
        rows2 = [(u, i + 1000 if u == ids[-1] else i) for i, u in enumerate(ids)]
        for t, rows in ((self.src_table, rows1), (self.dst_table, rows2)):
            for row in rows:  # one at a time, so the sample is the first rows inserted
                self.connection.query(t.insert_row(*row))
        self.connection.query(commit)
        segments = [
            table_segment(self.connection, path, "id", extra_columns=("v",), case_sensitive=False)
            for path in (self.table_src_path, self.table_dst_path)
        ]
        diff = list(HashDiffer(bisection_factor=2, bisection_threshold=100).diff_tables(*segments))
        self.assertEqual(sorted([("-", (ids[-1], "99")), ("+", (ids[-1], "1099"))]), sorted(diff))


class TestLeafRowHashJSON(DiffTestCase):
    db_cls = db.PostgreSQL

    def test_json_with_different_spacing_isnt_reported(self):
        for path, value in ((self.table_src_path, '{"a": 1}'), (self.table_dst_path, '{"a":1}')):
            name = ".".join(path)
            self.connection.query(f"CREATE TABLE {name} (id INT, j JSON)", None)
            self.connection.query(f"INSERT INTO {name} VALUES (1, '{value}'), (2, '[1, 2]')", None)
        self.connection.query(commit)
        segments = [
            table_segment(self.connection, path, "id", extra_columns=("j",), case_sensitive=False)
            for path in (self.table_src_path, self.table_dst_path)
        ]
        differ = HashDiffer(bisection_factor=2, bisection_threshold=100)
        self.assertEqual([], list(differ.diff_tables(*segments)))
        # The row hashes differ, so the row is fetched, and diff_sets() finds the JSON equal
        self.assertEqual(1, differ.stats["rows_fetched_by_key"])


def capture_sql(connection) -> tuple:
    """A patch that records each SQL statement the connection's class runs, and the list it fills."""
    statements = []
    cls = type(connection)
    original = cls._query

    def spy(self, sql_code):
        if isinstance(sql_code, str):
            statements.append(sql_code)
        return original(self, sql_code)

    return unittest.mock.patch.object(cls, "_query", spy), statements


class LeafSeparatorCase(DiffTestCase):
    """A leaf hashes each column that can hold '|' with its length in bytes in front, so that '|' that
    moves between neighboring columns ('a|b','c' against 'a','b|c') changes the row hash. The
    threshold is above the table size, so the root segment is the leaf."""

    key = ("id",)
    columns = ("c1", "c2", "c3")
    threshold = 4000

    # NULL, the text '<null>', the empty string, trailing spaces, separators and text outside ASCII
    TEXTS = ["a|b", "|", "||", "", "a  ", "é", "漢", "😀", "<null>", None]

    def segments(self):
        return [
            table_segment(self.connection, path, self.key, extra_columns=self.columns, case_sensitive=False)
            for path in (self.table_src_path, self.table_dst_path)
        ]

    def insert(self, rows1, rows2):
        self.connection.query([self.src_table.insert_rows(rows1), self.dst_table.insert_rows(rows2), commit])

    def diff(self, rows1=None, rows2=None):
        if rows1 is not None:
            self.insert(rows1, rows2)
        differ = HashDiffer(bisection_factor=2, bisection_threshold=self.threshold)
        diff = sorted(differ.diff_tables(*self.segments()))
        return diff, differ.stats

    def expected(self, rows1, rows2, keys) -> list:
        def as_text(row):
            return tuple(None if v is None else str(v) for v in row)

        key_size = len(self.key)
        diff = [("-", as_text(row)) for row in rows1 if tuple(row[:key_size]) in keys]
        diff += [("+", as_text(row)) for row in rows2 if tuple(row[:key_size]) in keys]
        return sorted(diff)


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashSeparator(LeafSeparatorCase):
    src_schema = {"id": int, "c1": str, "c2": str, "c3": str}
    dst_schema = {"id": int, "c1": str, "c2": str, "c3": str}

    def text_rows(self, n=30):
        t = self.TEXTS
        return [(i, t[i % 10], t[(i + 3) % 10], t[(i * 7) % 10]) for i in range(1, n + 1)]

    def plain_rows(self, n):
        return [(i, f"v{i}", "w", "x") for i in range(1, n + 1)]

    @staticmethod
    def swap(rows, changes: dict) -> list:
        return [(row[0], *changes[row[0]]) if row[0] in changes else row for row in rows]

    def test_equal_text_with_separators_fetches_nothing(self):
        rows = self.text_rows()
        diff, stats = self.diff(rows, rows)
        self.assertEqual([], diff)
        self.assertEqual(30, stats["rows_downloaded"])
        self.assertEqual(0, stats["rows_fetched_by_key"])

    def test_text_with_separators_that_differs_is_reported(self):
        rows1 = self.text_rows()
        rows2 = self.swap(rows1, {7: ("a|b", "", "😀 "), 12: ("|", "||", None)})
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(7,), (12,)}), diff)
        self.assertEqual(2, stats["rows_fetched_by_key"])

    def test_prefixed_hash_is_the_python_reference(self):
        # The same reference on both databases is what lets a Db2 leaf agree with a PostgreSQL one
        rows = self.text_rows()
        self.insert(rows, rows)
        segment = self.segments()[0].with_schema()
        hashes = segment.get_key_hashes(null_mask=True, length_columns=self.columns)

        def prefixed(value):
            return "<null>" if value is None else f"{len(value.encode('utf-8'))}:{value}"

        expected = {
            str(row[0]): str_to_checksum("|".join([str(row[0])] + [prefixed(v) for v in row[1:]])) for row in rows
        }
        self.assertEqual(expected, {key: row_hash for key, row_hash, *_ in hashes})

    def test_separator_moving_between_columns_is_reported(self):
        rows1 = self.swap(self.plain_rows(2000), {500: ("a|b", "c", "x")})
        rows2 = self.swap(rows1, {500: ("a", "b|c", "x")})
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(500,)}), diff)
        self.assertEqual(1, stats["rows_fetched_by_key"])

    def test_other_moved_separators_are_reported(self):
        pairs = {
            100: (("|", "", "x"), ("", "|", "x")),
            900: (("x|", "|y", "x"), ("x", "||y", "x")),
            1500: (("a|b", "c", "x"), ("a", "b|c", "x")),
            1700: (("v", "p|", "q"), ("v", "p", "|q")),
        }
        rows1 = self.swap(self.plain_rows(2000), {k: a for k, (a, _) in pairs.items()})
        rows2 = self.swap(rows1, {k: b for k, (_, b) in pairs.items()})
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(k,) for k in pairs}), diff)
        self.assertEqual(4, stats["rows_fetched_by_key"])

    def test_most_rows_with_moved_separators_download_every_row(self):
        # More suspects than one query fetches, and more than half the rows
        patch = unittest.mock.patch("data_diff.hashdiff_tables.LEAF_KEY_BATCH", 5)
        patch.start()
        self.addCleanup(patch.stop)
        rows1 = [(i, f"a|{i}", "b", "c") for i in range(1, 21)]
        rows2 = [(i, "a", f"{i}|b", "c") if i <= 15 else rows1[i - 1] for i in range(1, 21)]
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(i,) for i in range(1, 16)}), diff)
        self.assertEqual(0, stats.get("rows_fetched_by_key", 0))

    def test_every_row_with_a_moved_separator(self):
        rows1 = [(i, f"a|{i}", "b", "c") for i in range(1, 21)]
        rows2 = [(i, "a", f"{i}|b", "c") for i in range(1, 21)]
        diff, _ = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(i,) for i in range(1, 21)}), diff)


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashSeparatorCompositeKey(LeafSeparatorCase):
    src_schema = {"order_id": int, "line_number": int, "c1": str, "c2": str}
    dst_schema = {"order_id": int, "line_number": int, "c1": str, "c2": str}
    key = ("order_id", "line_number")
    columns = ("c1", "c2")

    def test_composite_key_separator(self):
        rows1 = [(o, n, f"o{o}|", f"|l{n}") for o in range(1, 51) for n in range(1, 5)]
        changes = {(17, 3): ("o17||", "l3"), (33, 2): ("changed", "|l2")}
        rows2 = [(o, n, *changes[(o, n)]) if (o, n) in changes else (o, n, a, b) for o, n, a, b in rows1]
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, set(changes)), diff)
        self.assertEqual(2, stats["rows_fetched_by_key"])


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashSeparatorNulls(LeafSeparatorCase):
    src_schema = {"id": int, "c1": str, "c2": str, "c3": str, "c4": str, "c5": str, "c6": str}
    dst_schema = {"id": int, "c1": str, "c2": str, "c3": str, "c4": str, "c5": str, "c6": str}
    columns = ("c1", "c2", "c3", "c4", "c5", "c6")

    def test_nulls_and_separators(self):
        values = [None, "<null>", "", "|", "<null>|", None]
        rows1 = [(i, *(values[(i + j) % 6] for j in range(6))) for i in range(1, 41)]
        rows1[19] = (20, "|", "", None, "<null>", None, "|")
        rows2 = list(rows1)
        rows2[19] = (20, "", "|", None, "<null>", None, "|")
        diff, stats = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(20,)}), diff)
        self.assertEqual(1, stats["rows_fetched_by_key"])


@test_each_database_in_list({db.PostgreSQL})
class TestLeafRowHashSeparatorNativeUUID(LeafSeparatorCase):
    src_schema = {"id": "uuid", "c1": str, "c2": str}
    dst_schema = {"id": "uuid", "c1": str, "c2": str}
    columns = ("c1", "c2")

    def test_native_uuid_key_separator(self):
        # The key range is wide, so the root bisects. Rows 3 and 4 are in the same half, and the
        # change in row 4 makes that half's checksum differ, which a moved '|' alone can't.
        ids = [str(uuid.UUID(int=i * 7919 + 12345)) for i in range(30)]
        rows1 = [(u, "a|b", "c") for u in ids]
        rows2 = list(rows1)
        rows2[3] = (ids[3], "a", "b|c")
        rows2[4] = (ids[4], "changed", "c")
        diff, _ = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(ids[3],), (ids[4],)}), diff)


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashTextUUIDKeySeparator(LeafSeparatorCase):
    """Text UUID keys that fetching by key can miss, with '|' moving between columns in that row."""

    src_schema = {"id": str, "c1": str, "c2": str}
    dst_schema = {"id": str, "c1": str, "c2": str}
    columns = ("c1", "c2")
    threshold = 100

    def test_mixed_case_key_with_a_moved_separator_is_reported(self):
        # Row 3's key is in uppercase, so fetching it by key misses it, and the segment downloads
        # every row. Row 4, in lowercase and in the same half, is what makes the half differ.
        ids = [str(uuid.UUID(int=i * 7919 + 12345)) for i in range(30)]
        ids = [u.upper() if i % 2 else u for i, u in enumerate(ids)]
        rows1 = [(u, "a|b", "c") for u in ids]
        rows2 = list(rows1)
        rows2[3] = (ids[3], "a", "b|c")
        rows2[4] = (ids[4], "changed", "c")
        diff, _ = self.diff(rows1, rows2)
        self.assertEqual(self.expected(rows1, rows2, {(ids[3],), (ids[4],)}), diff)

    def test_moved_separator_at_a_key_that_isnt_a_uuid_is_reported(self):
        # The sampled rows (the first 64) are UUIDs, so the key is a UUID type, and the last key
        # can't be made into one. Row 45 sorts next to it, and its change makes the segment differ.
        ids = [str(uuid.UUID(int=(i << 121) + i)) for i in range(1, 100)] + ["5zzz-not-a-uuid"]
        rows1 = [(u, "a|b", "c") for u in ids]
        rows2 = list(rows1)
        rows2[44] = (ids[44], "changed", "c")
        rows2[-1] = (ids[-1], "a", "b|c")
        for t, rows in ((self.src_table, rows1), (self.dst_table, rows2)):
            for row in rows:  # one at a time, so the sample is the first rows inserted
                self.connection.query(t.insert_row(*row))
        self.connection.query(commit)
        diff, _ = self.diff()
        self.assertEqual(self.expected(rows1, rows2, {(ids[44],), (ids[-1],)}), diff)


@test_each_database_in_list({db.PostgreSQL, db.Db2})
class TestLeafRowHashSeparatorNumericOnly(LeafSeparatorCase):
    """Columns that can't hold '|' keep the leaf SQL as it was."""

    src_schema = {"id": int, "d": "DECIMAL(10, 2)", "dt": "DATE", "ts": "TIMESTAMP", "b": "BOOLEAN"}
    dst_schema = {"id": int, "d": "DECIMAL(10, 2)", "dt": "DATE", "ts": "TIMESTAMP", "b": "BOOLEAN"}
    columns = ("d", "dt", "ts", "b")

    def test_numeric_only_leaf_sql_is_unchanged(self):
        for name, changed in ((self.table_src_name, "1.25"), (self.table_dst_name, "1.26")):
            q = self.connection.dialect.quote
            for i in range(1, 11):
                d = changed if i == 5 else f"{i}.5"
                self.connection.query(
                    f"INSERT INTO {q(name)} VALUES ({i}, {d}, '2026-01-0{i % 9 + 1}', "
                    f"'2026-01-02 03:04:0{i % 10}', {'TRUE' if i % 2 else 'FALSE'})",
                    None,
                )
        self.connection.query(commit)

        calls = []
        get_key_hashes = TableSegment.get_key_hashes

        def spy(segment, *args, **kwargs):
            calls.append((segment, kwargs))
            return get_key_hashes(segment, *args, **kwargs)

        patch_sql, statements = capture_sql(self.connection)
        with unittest.mock.patch.object(TableSegment, "get_key_hashes", spy), patch_sql:
            diff, stats = self.diff()
        self.assertEqual({("-", "5"), ("+", "5")}, {(sign, row[0]) for sign, row in diff})
        self.assertEqual(1, stats["rows_fetched_by_key"])
        self.assertEqual(2, len(calls))
        self.assertFalse([s for s in statements if "octet_length" in s.lower()])

        for segment, kwargs in calls:
            sqls = []
            for options in (kwargs, {"null_mask": True}):
                patch_sql, statements = capture_sql(self.connection)
                with patch_sql:
                    get_key_hashes(segment, **options)
                sqls.append(statements)
            self.assertEqual(sqls[0], sqls[1])


@test_each_database_in_list({db.Db2})
class TestLeafRowHashDb2Limit(LeafSeparatorCase):
    """Db2 fails a concatenation over 32,672 bytes (SQL0137N), so a row that fits only without the
    lengths makes the segment download every row."""

    src_schema = {"id": int, "c1": "VARCHAR(32000)", "c2": "VARCHAR(700)"}
    dst_schema = {"id": int, "c1": "VARCHAR(32000)", "c2": "VARCHAR(700)"}
    columns = ("c1", "c2")

    def test_hash_input_near_db2_limit_downloads_every_row(self):
        # Row 1 hashes 1 + 1 + 32,000 + 1 + 660 = 32,663 bytes, and 32,673 with '32000:' and '660:'
        q = self.connection.dialect.quote
        for name, changed in ((self.table_src_name, "y"), (self.table_dst_name, "z")):
            self.connection.query(
                f"INSERT INTO {q(name)} VALUES (1, REPEAT('a', 32000), REPEAT('b', 660)), (2, 'x', '{changed}')",
                None,
            )
        self.connection.query(commit)
        with self.assertLogs("hashdiff_tables", "INFO") as logs:
            diff, stats = self.diff()
        self.assertEqual([("+", ("2", "x", "z")), ("-", ("2", "x", "y"))], diff)
        self.assertEqual(0, stats.get("rows_fetched_by_key", 0))
        self.assertTrue([line for line in logs.output if "32,672" in line], logs.output)
