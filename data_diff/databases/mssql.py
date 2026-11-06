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
)
from data_diff.abcs.database_types import (
    JSON,
    ColType_UUID,
    FractionalType,
    Timestamp,
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
from data_diff.queries.ast_classes import Expr
from data_diff.queries.extras import NormalizeAsString


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
        "binary": Text,
        "varbinary": Text,
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
            return {bool: "bit", str: "text"}[t]
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
        return f"VALUES {values}"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if isinstance(coltype, Date):
            return f"CONVERT(VARCHAR(10), {value}, 23)"

        precision = coltype.precision
        if isinstance(coltype, Time):
            return f"CONVERT(VARCHAR(15), CAST(CAST({value} AS TIME({precision})) AS TIME(6)))"

        if isinstance(coltype, TimestampTZ):
            value = f"SWITCHOFFSET({value}, '+00:00')"

        if coltype.rounds:
            # Casting to a lower-precision DATETIME2 rounds. Style 121 keeps all 6 digits of DATETIME2(6).
            return f"CONVERT(VARCHAR(26), CAST(CAST({value} AS DATETIME2({precision})) AS DATETIME2(6)), 121)"

        timestamp6 = f"CONVERT(VARCHAR(26), CAST({value} AS DATETIME2(6)), 121)"
        return f"LEFT({timestamp6}, {TIMESTAMP_PRECISION_POS + precision}) + REPLICATE('0', {6 - precision})"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        if isinstance(coltype, Float):
            # A REAL cast directly to DECIMAL keeps only 6 or 7 significant digits.
            value = f"CAST({value} AS FLOAT)"
        return f"CONVERT(VARCHAR(50), CAST({value} AS DECIMAL(38, {coltype.precision})))"

    def normalize_uuid(self, value: str, coltype: ColType_UUID) -> str:
        if isinstance(coltype, Native_UUID):
            # SQL Server renders UNIQUEIDENTIFIER in uppercase, and other databases use lowercase.
            return f"LOWER({self.to_string(value)})"
        return super().normalize_uuid(value, coltype)

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
        info_schema_path = ["information_schema", "columns"]
        if database:
            info_schema_path.insert(0, self.dialect.quote(database))

        return (
            "SELECT column_name, data_type, datetime_precision, numeric_precision, numeric_scale, collation_name "
            f"FROM {'.'.join(info_schema_path)} "
            f"WHERE table_name = '{name}' AND table_schema = '{schema}'"
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
