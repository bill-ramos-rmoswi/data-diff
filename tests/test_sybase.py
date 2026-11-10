"""Driver behavior for SAP ASE: transactions, locks, errors, logins and literals."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest

from data_diff import connect, connect_to_table, diff_tables
from data_diff import databases as db
from data_diff.databases.base import QueryError
from data_diff.databases.sybase import Dialect, Sybase
from data_diff.diff_tables import Algorithm
from tests.common import CONN_STRINGS, random_table_suffix

READER = {"user": "dd_reader", "password": "Reader#1@x/y"}


def _raw(database):
    """The pyodbc connection behind a single-threaded Sybase instance."""
    return database._queue.submit(lambda: database.thread_local.conn).result()


class TestDialect(unittest.TestCase):
    def test_converts_a_timestamp_to_utc(self):
        value = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual("'2024-01-01 05:00:00'", Dialect().timestamp_value(value))

    def test_keeps_a_timestamp_without_a_time_zone(self):
        self.assertEqual(
            "'2024-01-01 00:00:00.123456'", Dialect().timestamp_value(datetime(2024, 1, 1, 0, 0, 0, 123456))
        )

    def test_text_outside_ascii_is_escaped(self):
        # The SQL text travels in cp850, which can't hold these characters
        self.assertEqual("U&'\\65E5\\672C'", Dialect()._constant_value("日本"))
        self.assertEqual("U&'it''s \\00E9 \\+01F600'", Dialect()._constant_value("it's é \U0001f600"))
        self.assertEqual("'it''s'", Dialect()._constant_value("it's"))

    def test_limit_goes_in_the_select(self):
        self.assertEqual("SELECT TOP 5 a FROM t", Dialect().limit_select("SELECT a FROM t", limit=5))
        self.assertEqual("SELECT DISTINCT TOP 5 a FROM t", Dialect().limit_select("SELECT DISTINCT a FROM t", limit=5))

    def test_temporary_tables_are_not_bracketed(self):
        self.assertEqual("#t", Dialect().quote("#t"))
        self.assertEqual("[t]", Dialect().quote("t"))


@unittest.skipUnless(db.Sybase in CONN_STRINGS, "Needs a configured Sybase ASE")
class TestSybaseDriver(unittest.TestCase):
    def setUp(self):
        self.db = connect(CONN_STRINGS[db.Sybase], shared=False)
        self.name = f"drv{random_table_suffix()}"
        self.db.query(f"CREATE TABLE {self.name} (id INT NOT NULL PRIMARY KEY, v INT NULL)", None)

    def tearDown(self):
        for suffix in ("", "_t", "_u"):
            self.db.query(f"IF object_id('{self.name}{suffix}') IS NOT NULL DROP TABLE {self.name}{suffix}", None)
        self.db.close()

    def _other(self):
        other = connect(CONN_STRINGS[db.Sybase], shared=False)
        self.addCleanup(other.close)
        return other

    def _locks_held(self, database) -> int:
        spid = database.query("SELECT @@spid", int)
        return self._other().query(f"SELECT count(*) FROM master..syslocks WHERE spid = {spid}", int)

    def test_autocommit_is_on(self):
        self.db.query(f"INSERT INTO {self.name} VALUES (1, 1)", None)
        self.assertEqual(0, self.db.query("SELECT @@tranchained", int))
        self.assertEqual(0, self.db.query("SELECT @@trancount", int))
        self.assertEqual(0, self._locks_held(self.db))
        # Another connection sees the row without waiting
        other = self._other()
        other.query("SET LOCK NOWAIT", None)
        self.assertEqual(1, other.query(f"SELECT count(*) FROM {self.name}", int))

    def test_a_failed_fetch_releases_its_locks(self):
        self.db.query("\n".join(f"INSERT INTO {self.name} VALUES ({i}, {i})" for i in range(2000)), None)
        failing = f"SELECT CASE WHEN id > 1500 THEN convert(int, 'boom ' + convert(varchar(9), id)) ELSE id END FROM {self.name}"
        # pyodbc raises DataError while fetching, and the driver reports it as a QueryError
        with self.assertRaisesRegex(QueryError, "boom 1501"):
            self.db.query(failing, list)
        self.assertEqual(0, self._locks_held(self.db))
        other = self._other()
        other.query("SET LOCK WAIT 2", None)
        other.query(f"UPDATE {self.name} SET v = 0 WHERE id = 1999", None)
        other.query(f"DROP TABLE {self.name}", None)

    def test_every_statement_of_a_batch_runs(self):
        # ASE cancels statements whose results aren't read, without an error
        self.db.query("\n".join(f"INSERT INTO {self.name} VALUES ({i}, {i})" for i in range(1000)), None)
        self.assertEqual(1000, self.db.query(f"SELECT count(*) FROM {self.name}", int))

    def test_an_error_later_in_a_batch_is_reported(self):
        batch = f"INSERT INTO {self.name} VALUES (1, 1)\nINSERT INTO {self.name} VALUES (1, 2)"
        with self.assertRaisesRegex(QueryError, "duplicate key"):
            self.db.query(batch, None)

    def test_ddl_runs_outside_a_transaction(self):
        # ASE refuses some DDL in a transaction unless 'ddl in tran' is on, which isn't needed in unchained mode
        self.db.query(f"CREATE INDEX ix_{self.name} ON {self.name} (v)", None)
        self.db.query(f"SELECT * INTO #copy_{self.name[-5:]} FROM {self.name}", None)
        self.assertEqual(0, self.db.query("SELECT @@trancount", int))

    def test_cp850_text_round_trips(self):
        text = bytes(range(32, 127)).decode("ascii") + bytes(range(128, 256)).decode("cp850")
        self.db.query(f"CREATE TABLE {self.name}_t (v VARCHAR(255) NULL)", None)
        self.db.query(f"INSERT INTO {self.name}_t VALUES ({self.db.dialect._constant_value(text)})", None)
        self.assertEqual(text, self.db.query(f"SELECT v FROM {self.name}_t", list)[0][0])
        # The server holds cp850 bytes, which the client converts
        self.assertEqual("82", self.db.query("SELECT bintostr(convert(varbinary(1), 'é'))", list)[0][0])

    def test_unsigned_int_values_come_back_as_strings(self):
        self.db.query(f"CREATE TABLE {self.name}_u (k UNSIGNED INT NOT NULL)", None)
        self.db.query(f"INSERT INTO {self.name}_u VALUES (4294967295)", None)
        segment = connect_to_table(CONN_STRINGS[db.Sybase], f"{self.name}_u", "k", thread_count=2)
        try:
            # FreeTDS returns unsigned int as a signed value, so key bounds are fetched as text
            self.assertEqual((("4294967295",), ("4294967295",)), segment.with_schema().query_key_range())
        finally:
            segment.database.close()


@unittest.skipUnless(db.Sybase in CONN_STRINGS, "Needs a configured Sybase ASE")
class TestSybaseLogins(unittest.TestCase):
    def setUp(self):
        self.admin = connect(CONN_STRINGS[db.Sybase], shared=False)
        self.name = f"ro{random_table_suffix()}"
        self.admin.query(f"CREATE TABLE {self.name} (id INT NOT NULL PRIMARY KEY, v VARCHAR(10) NULL)", None)
        self.admin.query(f"INSERT INTO {self.name} VALUES (1, 'a')\nINSERT INTO {self.name} VALUES (2, 'b')", None)
        self.admin.query(f"GRANT SELECT ON {self.name} TO dd_reader", None)

    def tearDown(self):
        self.admin.query(f"DROP TABLE {self.name}", None)
        self.admin.close()

    def _reader(self):
        # A dict, because a URI would need the password's @, / and # escaped, and URIs aren't decoded
        conf = {"driver": "sybase", "host": "ase", "port": 5000, "database": "testdb", "schema": "dbo", **READER}
        reader = connect(conf, shared=False)
        self.addCleanup(reader.close)
        return reader

    def test_dict_connection_with_special_characters_in_the_password(self):
        self.assertEqual("dd_reader", self._reader().query("SELECT suser_name()", list)[0][0])

    def test_read_only_login_can_diff(self):
        reader = self._reader()
        self.assertEqual(2, reader.query(f"SELECT count(*) FROM dbo.{self.name}", int))
        conf = {"driver": "sybase", "host": "ase", "port": 5000, "database": "testdb", "schema": "dbo", **READER}
        segments = [connect_to_table(conf, self.name, "id", thread_count=2, extra_columns=("v",)) for _ in range(2)]
        try:
            self.assertEqual([], list(diff_tables(*segments, algorithm=Algorithm.HASHDIFF)))
        finally:
            for segment in segments:
                segment.database.close()

    def test_read_only_login_can_not_write(self):
        reader = self._reader()
        with self.assertRaisesRegex(QueryError, "permission"):
            reader.query(f"INSERT INTO dbo.{self.name} VALUES (3, 'c')", None)
        with self.assertRaisesRegex(QueryError, "permission"):
            reader.query(f"CREATE TABLE {self.name}_w (id INT)", None)
        self.assertEqual(2, self.admin.query(f"SELECT count(*) FROM {self.name}", int))

    def test_bad_password_is_a_connect_error(self):
        conf = {
            "driver": "sybase",
            "host": "ase",
            "port": 5000,
            "database": "testdb",
            "user": "dd_reader",
            "password": "x",
        }
        with self.assertRaises(db.ConnectError):
            connect(conf, shared=False).query("SELECT 1", int)


@unittest.skipUnless(db.Sybase in CONN_STRINGS, "Needs a configured Sybase ASE")
class TestSybaseConcurrency(unittest.TestCase):
    def test_unicode_hashing_in_parallel(self):
        # Each thread compiles its own copy of the per-character conversion, which uses procedure cache
        admin = connect(CONN_STRINGS[db.Sybase], shared=False)
        names = [f"par{random_table_suffix()}" for _ in range(2)]
        try:
            for name in names:
                admin.query(f"CREATE TABLE {name} (id INT NOT NULL PRIMARY KEY, v UNIVARCHAR(255) NULL)", None)
                rows = "\n".join(f"INSERT INTO {name} VALUES ({i}, U&'\\65E5{i}')" for i in range(400))
                admin.query(rows, None)
            admin.query(f"UPDATE {names[1]} SET v = U&'\\672C' WHERE id = 7", None)

            def run(_):
                segments = [
                    connect_to_table(CONN_STRINGS[db.Sybase], n, "id", thread_count=8, extra_columns=("v",))
                    for n in names
                ]
                try:
                    return list(
                        diff_tables(*segments, algorithm=Algorithm.HASHDIFF, bisection_factor=4, bisection_threshold=16)
                    )
                finally:
                    for segment in segments:
                        segment.database.close()

            with ThreadPoolExecutor(2) as pool:
                for result in pool.map(run, range(2)):
                    self.assertEqual(2, len(result))
        finally:
            for name in names:
                admin.query(f"IF object_id('{name}') IS NOT NULL DROP TABLE {name}", None)
            admin.close()


class TestSybaseClass(unittest.TestCase):
    def test_connection_string_escapes_values_that_need_it(self):
        database = Sybase(host="ase", user="u", password="p;w}=x", database="d", thread_count=1)
        try:
            self.assertIn("PWD={p;w}}=x}", database._conn_str)
            self.assertIn("ClientCharset=UTF-8", database._conn_str)
        finally:
            database.close()
