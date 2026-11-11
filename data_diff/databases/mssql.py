from datetime import datetime
from typing import Any, ClassVar, Dict, Optional, Type

import attrs

from data_diff.databases.base import (
    CHECKSUM_HEXDIGITS,
    CHECKSUM_OFFSET,
    TIMESTAMP_PRECISION_POS,
    QueryError,
    ThreadedDatabase,
    import_helper,
    ConnectError,
    BaseDialect,
    Compiler,
)
from data_diff.abcs.database_types import (
    JSON,
    Binary,
    DigestSource,
    StringType,
    ColType_UUID,
    FractionalType,
    Timestamp,
    LongText,
    TimestampTZ,
    DbPath,
    Float,
    Decimal,
    Integer,
    TemporalType,
    Native_UUID,
    Text,
    Boolean,
    ColType,
    Date,
    Time,
)
from data_diff.queries.ast_classes import Code, Concat, CreateTable, Expr
from data_diff.queries.extras import NormalizeAsString
from data_diff.utils import BINARY_DIGEST_CHUNK, TEXT_DIGEST_CHUNK, join_iter


@import_helper("mssql")
def import_mssql():
    import pyodbc

    return pyodbc


@attrs.define(frozen=False)
class Dialect(BaseDialect):
    name = "MsSQL"
    ROUNDS_ON_PREC_LOSS = True
    SUPPORTS_PRIMARY_KEY: ClassVar[bool] = True
    SUPPORTS_INDEXES = True
    TYPE_CLASSES = {
        # Timestamps
        "datetimeoffset": TimestampTZ,
        "datetime": Timestamp,
        "datetime2": Timestamp,
        "smalldatetime": Timestamp,
        "date": Date,
        "time": Time,
        # Numbers
        "float": Float,
        "real": Float,
        "decimal": Decimal,
        "money": Decimal,
        "smallmoney": Decimal,
        # int
        "int": Integer,
        "bigint": Integer,
        "tinyint": Integer,
        "smallint": Integer,
        # Text
        "varchar": Text,
        "char": Text,
        "text": Text,
        "ntext": Text,
        "nvarchar": Text,
        "nchar": Text,
        # Bytes. timestamp and rowversion are row versions, which a copy of the row doesn't keep.
        "binary": Binary,
        "varbinary": Binary,
        "image": Binary,
        "xml": Text,
        # UUID
        "uniqueidentifier": Native_UUID,
        # Bool
        "bit": Boolean,
        # JSON
        "json": JSON,
    }

    def quote(self, s: str) -> str:
        return f"[{s}]"

    def _convert_db_precision_to_digits(self, p: int) -> int:
        # Same reduction as PostgreSQL, so a REAL and a DOUBLE PRECISION holding
        # the same number compare equal despite their binary rounding.
        return max(super()._convert_db_precision_to_digits(p) - 2, 0)

    def key_range_expr(self, column: Expr, coltype: Optional[ColType]) -> Expr:
        if isinstance(coltype, Native_UUID):
            # UNIQUEIDENTIFIER doesn't sort like a string (the last 6 bytes are the most
            # significant), so comparing it directly selects different rows than other
            # databases. Comparing the text is correct, but can't use an index.
            return NormalizeAsString(column, coltype)
        return column

    def set_timezone_to_utc(self) -> str:
        raise NotImplementedError("MsSQL does not support a session timezone setting.")

    def current_timestamp(self) -> str:
        return "GETDATE()"

    def current_database(self) -> str:
        return "DB_NAME()"

    def current_schema(self) -> str:
        return """default_schema_name
        FROM sys.database_principals
        WHERE name = CURRENT_USER"""

    def to_string(self, s: str) -> str:
        # NVARCHAR, because converting Unicode text to VARCHAR loses characters
        # outside the database's code page.
        return f"CONVERT(NVARCHAR(MAX), {s})"

    def type_repr(self, t) -> str:
        try:
            # TIMESTAMP means a row version in SQL Server, not a point in time
            return {
                bool: "bit",
                # NVARCHAR(MAX) can't be part of a key
                str: "NVARCHAR(4000)",
                LongText: "NVARCHAR(MAX)",
                datetime: "DATETIME2(6)",
                float: "FLOAT",
            }[t]
        except KeyError:
            return super().type_repr(t)

    def random(self) -> str:
        return "rand()"

    def is_distinct_from(self, a: str, b: str) -> str:
        # IS (NOT) DISTINCT FROM is available only since SQLServer 2022.
        # See: https://stackoverflow.com/a/18684859/857383
        return f"(({a}<>{b} OR {a} IS NULL OR {b} IS NULL) AND NOT({a} IS NULL AND {b} IS NULL))"

    def limit_select(
        self,
        select_query: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        has_order_by: Optional[bool] = None,
    ) -> str:
        if offset:
            raise NotImplementedError("No support for OFFSET in query")
        result = ""
        if not has_order_by:
            result += "ORDER BY 1"

        result += f" OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"

        # mssql requires that subquery columns are all aliased, so
        # don't wrap in an outer select
        return f"{select_query} {result}"

    def constant_values(self, rows) -> str:
        values = ", ".join("(%s)" % ", ".join(self._constant_value(v) for v in row) for row in rows)
        # A plain VALUES clause allows at most 1000 rows, and a derived table has no such limit.
        columns = ", ".join(f"c{i}" for i in range(len(rows[0])))
        return f"SELECT * FROM (VALUES {values}) AS constant_values({columns})"

    def render_createtable(self, c: Compiler, elem: CreateTable) -> str:
        path = self.compile(c, elem.path)
        if elem.source_table:
            # SQL Server has no CREATE TABLE ... AS SELECT
            statement = f"SELECT * INTO {path} FROM ({self.compile(c, elem.source_table)}) AS source"
        else:
            statement = super().render_createtable(c, attrs.evolve(elem, if_not_exists=False))

        # SQL Server has no CREATE TABLE IF NOT EXISTS
        return f"IF OBJECT_ID('{path}') IS NULL {statement}" if elem.if_not_exists else statement

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if isinstance(coltype, Date):
            return f"CONVERT(VARCHAR(10), {value}, 23)"

        precision = coltype.precision
        if isinstance(coltype, Time):
            if coltype.rounds:
                return f"CONVERT(VARCHAR(15), CAST(CAST({value} AS TIME({precision})) AS TIME(6)))"
            time7 = f"CONVERT(VARCHAR(16), CAST({value} AS TIME(7)))"
            digits = min(precision, 6)
            return f"LEFT({time7}, {len('12:24:35.') + digits}) + REPLICATE('0', {6 - digits})"

        if isinstance(coltype, TimestampTZ):
            value = f"SWITCHOFFSET({value}, '+00:00')"

        if coltype.rounds:
            # Casting to a lower-precision DATETIME2 rounds. Style 121 keeps all 6 digits of DATETIME2(6).
            return f"CONVERT(VARCHAR(26), CAST(CAST({value} AS DATETIME2({precision})) AS DATETIME2(6)), 121)"

        # The other side truncated. Casting to DATETIME2(6) would round, so keep all the digits to cut them.
        timestamp7 = f"CONVERT(VARCHAR(27), CAST({value} AS DATETIME2(7)), 121)"
        digits = min(precision, 6)
        return f"LEFT({timestamp7}, {TIMESTAMP_PRECISION_POS + digits}) + REPLICATE('0', {6 - digits})"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        if isinstance(coltype, Float):
            # A REAL cast directly to DECIMAL keeps only 6 or 7 significant digits.
            value = f"CAST({value} AS FLOAT)"
        return f"CONVERT(VARCHAR(50), CAST({value} AS DECIMAL(38, {coltype.precision})))"

    def normalize_uuid(self, value: str, coltype: ColType_UUID) -> str:
        if isinstance(coltype, Native_UUID):
            # SQL Server renders UNIQUEIDENTIFIER in uppercase, and other databases use lowercase.
            return f"LOWER(CONVERT(CHAR(36), {value}))"
        return super().normalize_uuid(value, coltype)

    def normalize_value_by_type(self, value: str, coltype: ColType) -> str:
        # NVARCHAR(MAX) makes checksums about 3 times slower, so only text pays for it
        if isinstance(coltype, Integer):
            return f"CONVERT(VARCHAR(50), {value})"
        return super().normalize_value_by_type(value, coltype)

    def render_concat(self, c: Compiler, elem: Concat) -> str:
        if self._prevent_overflow_when_concat:
            return super().render_concat(c, elem)

        def as_string(expr) -> str:
            # Checksums pass normalized strings, which to_string() would only slow down. Other
            # callers pass raw columns, and coalesce() can't mix an INT with '<null>'.
            compiled = self.compile(c, expr)
            return compiled if isinstance(expr, (Code, NormalizeAsString)) else self.to_string(compiled)

        items = [f"coalesce({as_string(expr)}, '<null>')" for expr in elem.exprs]
        if len(items) == 1:
            return items[0]
        if elem.sep:
            items = list(join_iter(f"'{elem.sep}'", items))
        return self.concat(items)

    def _hash_md5(self, s: str) -> str:
        # Other databases hash UTF-8 bytes. HASHBYTES hashes the bytes of the value it receives,
        # which would otherwise be UTF-16 or the database's code page.
        utf8 = f"CONVERT(VARCHAR(MAX), CONVERT(NVARCHAR(MAX), {s}) COLLATE Latin1_General_100_BIN2_UTF8)"
        return f"HASHBYTES('MD5', {utf8})"

    def md5_as_int(self, s: str) -> str:
        hex_digest = f"CONVERT(VARCHAR(32), {self._hash_md5(s)}, 2)"
        return (
            f"CONVERT(BIGINT, CONVERT(VARBINARY(8), '0x' + RIGHT({hex_digest}, {CHECKSUM_HEXDIGITS}), 1))"
            f" - {CHECKSUM_OFFSET}"
        )

    def md5_as_hex(self, s: str) -> str:
        return f"LOWER(CONVERT(VARCHAR(32), {self._hash_md5(s)}, 2))"

    # Large values. HASHBYTES takes a value of any size, but the digest must match databases
    # that can only hash a piece at a time, so it's computed in the same pieces.

    # SQL Server can't aggregate over an expression that contains a subquery
    DIGEST_IN_SUBQUERY: ClassVar[bool] = True

    def as_digest_type(self, coltype: ColType, source: DigestSource) -> ColType:
        if isinstance(coltype, (StringType, ColType_UUID)) and not isinstance(coltype, Native_UUID):
            # Whatever sampling refined the column to, the other side holds plain text
            return Text(digest=source)
        return super().as_digest_type(coltype, source)

    @staticmethod
    def _digest_sum(md5: str, chunk: str, last_chunk: str) -> str:
        # The last 6 bytes of the MD5, which are the 12 hex digits other databases read. The sum
        # is over a derived table, because an aggregate can't mix an outer column with s.value.
        # GENERATE_SERIES needs SQL Server 2022.
        term = f"CASE WHEN DATALENGTH({chunk}) > 0 THEN CONVERT(BIGINT, SUBSTRING({md5}, 11, 6)) ELSE 0 END"
        return (
            f"(SELECT CONVERT(VARCHAR(30), SUM(x.term)) FROM (SELECT {term} AS term"
            f" FROM GENERATE_SERIES(CONVERT(BIGINT, 0), CONVERT(BIGINT, {last_chunk})) s) x)"
        )

    def normalize_digest(self, value: str, coltype: ColType) -> str:
        if isinstance(coltype, Binary):
            n = BINARY_DIGEST_CHUNK
            v = f"CONVERT(VARBINARY(MAX), {value})"
            chunk = f"SUBSTRING({v}, s.value * {n} + 1, {n})"
            md5 = f"HASHBYTES('MD5', CONVERT(VARBINARY(13), CONVERT(VARCHAR(12), s.value) + '|') + {chunk})"
            chunks = self._digest_sum(md5, chunk, f"(DATALENGTH({v}) - 1) / {n}")
            return f"CASE WHEN {value} IS NULL THEN NULL WHEN DATALENGTH({v}) = 0 THEN '0' ELSE {chunks} END"

        n = TEXT_DIGEST_CHUNK
        v = f"CONVERT(NVARCHAR(MAX), {value})"

        def chunks_of(text: str) -> str:
            chunk = f"SUBSTRING({text}, s.value * {n} + 1, {n})"
            md5 = self._hash_md5(f"CONVERT(NVARCHAR(12), s.value) + N'|' + {chunk}")
            # A value with surrogate pairs has fewer characters than this, and the chunks past
            # its end are empty
            return self._digest_sum(md5, chunk, f"(DATALENGTH({v}) / 2 - 1) / {n}")

        # Other databases cut text by character. NVARCHAR is cut by UTF-16 code unit, which is
        # the same unless the text has surrogate pairs. A collation that knows them cuts by
        # character, but has to read the text from its start for each chunk.
        has_surrogates = f"{v} COLLATE Latin1_General_BIN2 LIKE N'%[' + NCHAR(55296) + N'-' + NCHAR(56319) + N']%'"
        by_character = chunks_of(f"{v} COLLATE Latin1_General_100_CI_AS_SC")
        return (
            f"CASE WHEN {value} IS NULL THEN NULL WHEN DATALENGTH({v}) = 0 THEN '0'"
            f" WHEN {has_surrogates} THEN {by_character} ELSE {chunks_of(v)} END"
        )


@attrs.define(frozen=False, init=False, kw_only=True)
class MsSQL(ThreadedDatabase):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = "mssql://<user>:<password>@<host>/<database>/<schema>"
    CONNECT_URI_PARAMS = ["database", "schema"]

    default_database: str
    _args: Dict[str, Any]
    _mssql: Any

    def __init__(self, host, port, user, password, *, database, thread_count, **kw) -> None:
        super().__init__(thread_count=thread_count)

        # ODBC takes the port as part of the server name
        server = f"{host},{port}" if port else host
        args = dict(server=server, database=database, user=user, password=password, **kw)
        self._args = {k: v for k, v in args.items() if v is not None}
        self._args["driver"] = "{ODBC Driver 18 for SQL Server}"

        try:
            self.default_database = self._args["database"]
            self.default_schema = self._args["schema"]
        except KeyError:
            raise ValueError("Specify a default database and schema.")

        self._mssql = None

    def create_connection(self):
        self._mssql = import_mssql()
        try:
            connection = self._mssql.connect(**self._args)
            return connection
        except self._mssql.Error as error:
            raise ConnectError(*error.args) from error

    def select_table_schema(self, path: DbPath) -> str:
        """Provide SQL for selecting the table schema as (name, type, date_prec, num_prec)"""
        database, schema, name = self._normalize_table_path(path)
        # In uppercase, because a database with a case-sensitive collation is strict about these names
        info_schema_path = ["INFORMATION_SCHEMA", "COLUMNS"]
        if database:
            info_schema_path.insert(0, self.dialect.quote(database))

        return (
            "SELECT COLUMN_NAME, DATA_TYPE, DATETIME_PRECISION, NUMERIC_PRECISION, NUMERIC_SCALE, COLLATION_NAME "
            f"FROM {'.'.join(info_schema_path)} "
            f"WHERE TABLE_NAME = '{name}' AND TABLE_SCHEMA = '{schema}'"
        )

    def _normalize_table_path(self, path: DbPath) -> DbPath:
        if len(path) == 1:
            return self.default_database, self.default_schema, path[0]
        elif len(path) == 2:
            return self.default_database, path[0], path[1]
        elif len(path) == 3:
            return path

        raise ValueError(
            f"{self.name}: Bad table path for {self}: '{'.'.join(path)}'. Expected format: table, schema.table, or database.schema.table"
        )

    def _query_cursor(self, c, sql_code: str):
        try:
            return super()._query_cursor(c, sql_code)
        except self._mssql.DatabaseError as e:
            raise QueryError(e)
