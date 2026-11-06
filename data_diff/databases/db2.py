from typing import Any, ClassVar, Dict, List, Optional, Type

import attrs

from data_diff.abcs.database_types import (
    Boolean,
    Date,
    DbPath,
    DbTime,
    Decimal,
    Float,
    FractionalType,
    Integer,
    TemporalType,
    Text,
    Time,
    Timestamp,
)
from data_diff.databases.base import (
    CHECKSUM_HEXDIGITS,
    CHECKSUM_OFFSET,
    MD5_HEXDIGITS,
    TIMESTAMP_PRECISION_POS,
    BaseDialect,
    Compiler,
    ConnectError,
    QueryError,
    ThreadedDatabase,
    import_helper,
)
from data_diff.queries.ast_classes import CreateTable, TruncateTable


@import_helper("db2")
def import_db2():
    import ibm_db_dbi

    return ibm_db_dbi


@attrs.define(frozen=False)
class Dialect(BaseDialect):
    name = "Db2"
    # Db2 truncates: casting 10.123456 to TIMESTAMP(3) gives 10.123000
    ROUNDS_ON_PREC_LOSS = False
    # SELECT needs a FROM clause
    PLACEHOLDER_TABLE = "SYSIBM.SYSDUMMY1"
    TYPE_CLASSES = {
        # Timestamps
        "TIMESTAMP": Timestamp,
        "DATE": Date,
        "TIME": Time,
        # Numbers
        "DOUBLE": Float,
        "REAL": Float,
        "DECFLOAT": Float,
        "DECIMAL": Decimal,
        # Integers
        "SMALLINT": Integer,
        "INTEGER": Integer,
        "BIGINT": Integer,
        # Text
        "VARCHAR": Text,
        "CHARACTER": Text,
        "CLOB": Text,
        "LONG VARCHAR": Text,
        "GRAPHIC": Text,
        "VARGRAPHIC": Text,
        "DBCLOB": Text,
        # Bool
        "BOOLEAN": Boolean,
    }

    def quote(self, s: str) -> str:
        return f'"{s}"'

    def _convert_db_precision_to_digits(self, p: int) -> int:
        # Same reduction as PostgreSQL, so that REAL and DOUBLE compare equal
        # despite their binary rounding.
        return max(super()._convert_db_precision_to_digits(p) - 2, 0)

    def set_timezone_to_utc(self) -> str:
        raise NotImplementedError("Db2 does not support a session timezone setting.")

    def current_timestamp(self) -> str:
        return "CURRENT TIMESTAMP"

    def current_database(self) -> str:
        return "CURRENT SERVER"

    def current_schema(self) -> str:
        return "CURRENT SCHEMA"

    def random(self) -> str:
        return "RAND()"

    def to_string(self, s: str) -> str:
        return f"VARCHAR({s})"

    def concat(self, items: List[str]) -> str:
        assert len(items) > 1
        return " || ".join(items)

    def type_repr(self, t) -> str:
        try:
            return {str: "VARCHAR(4000)", float: "DOUBLE"}[t]
        except KeyError:
            return super().type_repr(t)

    def timestamp_value(self, t: DbTime) -> str:
        return f"TIMESTAMP('{t.replace(tzinfo=None).isoformat(sep=' ')}')"

    def explain_as_text(self, query: str) -> str:
        raise NotImplementedError("Db2 explains a query into tables, not as text.")

    def limit_select(
        self,
        select_query: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        has_order_by: Optional[bool] = None,
    ) -> str:
        offset_clause = f" OFFSET {offset} ROWS" if offset else ""
        return f"{select_query}{offset_clause} FETCH FIRST {limit} ROWS ONLY"

    def constant_values(self, rows) -> str:
        values = ", ".join("(%s)" % ", ".join(self._constant_value(v) for v in row) for row in rows)
        return f"VALUES {values}"

    def render_createtable(self, c: Compiler, elem: CreateTable) -> str:
        if elem.source_table:
            ne = "IF NOT EXISTS " if elem.if_not_exists else ""
            return f"CREATE TABLE {ne}{self.compile(c, elem.path)} AS ({self.compile(c, elem.source_table)}) WITH DATA"
        return super().render_createtable(c, elem)

    def render_truncatetable(self, c: Compiler, elem: TruncateTable) -> str:
        return f"TRUNCATE TABLE {self.compile(c, elem.path)} IMMEDIATE"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if isinstance(coltype, Date):
            return f"VARCHAR_FORMAT({value}, 'YYYY-MM-DD')"

        if isinstance(coltype, Time):
            # TIME has no fractional seconds in Db2
            return f"VARCHAR({value}, JIS) || '.000000'"

        # TIMESTAMP goes up to 12 digits, and we compare at most 6
        precision = min(coltype.precision, 6)
        if coltype.rounds and precision < 6:
            half_of_last_digit = 5 * 10 ** (5 - precision)
            value = f"({value} + {half_of_last_digit} MICROSECONDS)"

        timestamp6 = f"VARCHAR_FORMAT({value}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
        return f"RPAD(LEFT({timestamp6}, {TIMESTAMP_PRECISION_POS + precision}), {TIMESTAMP_PRECISION_POS + 6}, '0')"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        precision = coltype.precision
        if isinstance(coltype, Float):
            # Going through DECFLOAT keeps the digits of the shortest decimal form.
            # DOUBLE cast directly to DECIMAL truncates.
            value = f"DECFLOAT({value})"

        # ROUND because casting to DECIMAL also truncates. 31 is the most digits a DECIMAL can have.
        number = f"VARCHAR(CAST(ROUND({value}, {precision}) AS DECIMAL(31, {precision})))"

        # VARCHAR omits the leading zero of a decimal, as in .32 and -.32
        return f"REPLACE(REPLACE(REPLACE('#' || {number}, '#.', '0.'), '#-.', '-0.'), '#', '')"

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return f"CASE WHEN {value} THEN '1' WHEN NOT {value} THEN '0' END"

    def md5_as_int(self, s: str) -> str:
        # Db2 has no function that reads hex as a number, so add up the value of each digit.
        # The LATERAL query computes the hash once per row, instead of once per digit.
        # BIGINT is required: multiplying two INTs overflows for digits that are large enough.
        hex_digits = "'0123456789ABCDEF'"
        digit_values = " + ".join(
            f"BIGINT(LOCATE(SUBSTR(h, {i + 1}, 1), {hex_digits}) - 1) * {16 ** (CHECKSUM_HEXDIGITS - i - 1)}"
            for i in range(CHECKSUM_HEXDIGITS)
        )
        first_byte = (MD5_HEXDIGITS - CHECKSUM_HEXDIGITS) // 2 + 1
        last_bytes = f"SUBSTR(HASH({s}, 0), {first_byte}, {CHECKSUM_HEXDIGITS // 2})"
        return f"(SELECT ({digit_values}) - {CHECKSUM_OFFSET} FROM LATERAL(VALUES (HEX({last_bytes}))) AS md5_tail(h))"

    def md5_as_hex(self, s: str) -> str:
        return f"LOWER(HEX(HASH({s}, 0)))"


@attrs.define(frozen=False, init=False, kw_only=True)
class Db2(ThreadedDatabase):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = "db2://<user>:<password>@<host>:<port>/<database>/<schema>"
    CONNECT_URI_PARAMS = ["database", "schema?"]

    _dsn: str
    _db2: Any

    def __init__(self, host, user, password, *, database, schema=None, port=50000, thread_count, **kw) -> None:
        super().__init__(thread_count=thread_count)

        # Any other URI parameter, such as Security=SSL, is passed through to the driver.
        args: Dict[str, Any] = dict(
            DATABASE=database, HOSTNAME=host, PORT=port, PROTOCOL="TCPIP", UID=user, PWD=password, **kw
        )
        self._dsn = "".join(f"{k}={v};" for k, v in args.items())

        # Db2 stores unquoted schema names in uppercase, and a user's default schema is named after them.
        self.default_schema = (schema or user).upper()
        self._db2 = None

    @property
    def is_autocommit(self) -> bool:
        return True

    def create_connection(self):
        self._db2 = import_db2()
        try:
            connection = self._db2.connect(self._dsn, "", "")
            # ibm_db_dbi turns autocommit off by default. An uncommitted CREATE TABLE
            # keeps its locks on the catalog, which blocks every other connection.
            connection.set_autocommit(True)
            cursor = connection.cursor()
            cursor.execute(f"SET CURRENT SCHEMA = {self.dialect.quote(self.default_schema)}")
            cursor.close()
            return connection
        except self._db2.Error as error:
            raise ConnectError(*error.args) from error

    def select_table_schema(self, path: DbPath) -> str:
        """Provide SQL for selecting the table schema as (name, type, date_prec, num_prec, num_scale)"""
        schema, name = self._normalize_table_path(path)

        # Db2 has no information_schema. LENGTH holds the number of digits for DECIMAL, and bytes
        # for floating point, so those are translated to the binary precision that's expected.
        return (
            "SELECT COLNAME, TYPENAME, "
            "CASE WHEN TYPENAME = 'TIMESTAMP' THEN SCALE END, "
            "CASE WHEN TYPENAME = 'DOUBLE' THEN 53 "
            "WHEN TYPENAME = 'REAL' THEN 24 "
            "WHEN TYPENAME = 'DECFLOAT' AND LENGTH = 8 THEN 53 "
            "WHEN TYPENAME = 'DECFLOAT' THEN 113 "
            "WHEN TYPENAME = 'DECIMAL' THEN LENGTH END, "
            "CASE WHEN TYPENAME = 'DECIMAL' THEN SCALE END "
            "FROM SYSCAT.COLUMNS "
            f"WHERE TABSCHEMA = '{schema}' AND TABNAME = '{name}' "
            "ORDER BY COLNO"
        )

    def _query_cursor(self, c, sql_code: str):
        # ibm_db cuts one character off the end of a statement for each character outside the
        # Basic Multilingual Plane, such as an emoji in a key bound. Trailing spaces absorb that.
        sql_code += " " * sum(ord(char) > 0xFFFF for char in sql_code)
        try:
            return super()._query_cursor(c, sql_code)
        except self._db2.Error as e:
            raise QueryError(e)
