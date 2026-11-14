from contextlib import suppress
from datetime import timezone
from typing import Any, ClassVar, Dict, List, Optional, Type

import attrs

from data_diff.abcs.database_types import (
    Binary,
    Boolean,
    ColType,
    ColType_UUID,
    Date,
    DbPath,
    DbTime,
    Decimal,
    DigestSource,
    Float,
    FractionalType,
    Integer,
    LongText,
    Native_UUID,
    StringType,
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
from data_diff.queries.extras import LengthPrefixed
from data_diff.utils import BINARY_DIGEST_CHUNK, TEXT_DIGEST_CHUNK


# Chunks of a large value are counted with three digits in this base
DIGEST_BASE = 32
MAX_DIGEST_CHUNKS = DIGEST_BASE**3


@attrs.define(frozen=True)
class Db2Lob(Text):
    """CLOB and DBCLOB, which HASH rejects"""


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
    # Fetching rows through ibm_db_dbi is slow, about 9 times PostgreSQL's, and a key and a hash
    # are much less to fetch than every normalized column
    LEAF_ROW_HASH: ClassVar[bool] = True
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
        "CLOB": Db2Lob,
        "LONG VARCHAR": Text,
        "GRAPHIC": Text,
        "VARGRAPHIC": Text,
        "DBCLOB": Db2Lob,
        # Bytes
        "BLOB": Binary,
        "BINARY": Binary,
        "VARBINARY": Binary,
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
            return {str: "VARCHAR(4000)", LongText: "CLOB", float: "DOUBLE"}[t]
        except KeyError:
            return super().type_repr(t)

    def timestamp_value(self, t: DbTime) -> str:
        # TIMESTAMP has no time zone, so a value with one must be converted, not just stripped of it
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return f"TIMESTAMP('{t.isoformat(sep=' ')}')"

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

    def render_lengthprefixed(self, c: Compiler, elem: LengthPrefixed) -> str:
        # The database is UTF-8 (code page 1208), so OCTET_LENGTH agrees with PostgreSQL's. A row whose
        # hash input then exceeds 32,672 bytes fails with SQL0137N, and the leaf downloads its rows.
        # The length is taken of the VARCHAR that's hashed: a normalized GRAPHIC or VARGRAPHIC value,
        # such as TRIM(c) for a text UUID, stays graphic, and its OCTET_LENGTH counts UTF-16 bytes.
        value = self.to_string(self.compile(c, elem.expr))
        return f"(VARCHAR(OCTET_LENGTH({value})) || ':' || {value})"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if isinstance(coltype, Date):
            return f"VARCHAR_FORMAT({value}, 'YYYY-MM-DD')"

        if isinstance(coltype, Time):
            # TIME has no fractional seconds in Db2
            return f"VARCHAR({value}, JIS) || '.000000'"

        # TIMESTAMP goes up to 12 digits, and we compare at most 6
        precision = min(coltype.precision, 6)
        if coltype.rounds:
            if precision < 6:
                rounding = f"{5 * 10 ** (5 - precision)} MICROSECONDS"
            else:
                # TIMESTAMP(7) and above must round too, and half a microsecond isn't a valid duration
                seventh_digit = f"SUBSTR(VARCHAR_FORMAT({value}, 'FF7'), 7, 1)"
                rounding = f"(CASE WHEN {seventh_digit} >= '5' THEN 1 ELSE 0 END) MICROSECONDS"
            # Rounding up the last second of 9999, often used to mean "no end date", would overflow
            value = f"CASE WHEN {value} < TIMESTAMP('9999-12-31-23.59.59') THEN {value} + {rounding} ELSE {value} END"

        timestamp6 = f"VARCHAR_FORMAT({value}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
        return f"RPAD(LEFT({timestamp6}, {TIMESTAMP_PRECISION_POS + precision}), {TIMESTAMP_PRECISION_POS + 6}, '0')"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        precision = coltype.precision
        if isinstance(coltype, Float):
            # Going through DECFLOAT keeps the digits of the shortest decimal form, because DOUBLE
            # cast directly to DECIMAL truncates. Other databases use DECIMAL(38, precision), and
            # a Db2 DECIMAL has only 31 digits, which would overflow for large values.
            number = f"DECFLOAT({value})"
            whole_digits = 38 - precision
            number_format = "9" * (whole_digits - 1) + "0" + ("." + "0" * precision if precision else "")
            # Adding zero turns a rounded -0 into 0, which VARCHAR_FORMAT rejects
            fixed = f"LTRIM(VARCHAR_FORMAT(ROUND({number}, {precision}) + DECFLOAT(0), '{number_format}'))"
            # VARCHAR_FORMAT renders a value that doesn't fit as #####, and those would all hash the same
            return f"CASE WHEN ABS({number}) < 1E{whole_digits} THEN {fixed} ELSE VARCHAR(NORMALIZE_DECFLOAT({number})) END"

        # ROUND because casting to DECIMAL also truncates. 31 is the most digits a DECIMAL can have.
        number = f"VARCHAR(CAST(ROUND({value}, {precision}) AS DECIMAL(31, {precision})))"

        # VARCHAR omits the leading zero of a decimal, as in .32 and -.32
        return f"REPLACE(REPLACE(REPLACE('#' || {number}, '#.', '0.'), '#-.', '-0.'), '#', '')"

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return f"CASE WHEN {value} THEN '1' WHEN NOT {value} THEN '0' END"

    def md5_as_int(self, s: str) -> str:
        return self._md5_tail(s, CHECKSUM_OFFSET)

    def _md5_tail(self, s: str, offset: int = 0) -> str:
        """The last 12 hex digits of the MD5 of a string or bytes, as a number, less the offset."""
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
        return f"(SELECT ({digit_values}) - {offset} FROM LATERAL(VALUES (HEX({last_bytes}))) AS md5_tail(h))"

    # Large values. HASH rejects a CLOB, and takes at most 32,672 bytes, so a large value is
    # hashed a chunk at a time.

    def needs_digest(self, coltype: ColType) -> bool:
        return isinstance(coltype, (Binary, Db2Lob))

    def as_digest_type(self, coltype: ColType, source: DigestSource) -> ColType:
        if isinstance(coltype, (StringType, ColType_UUID)) and not isinstance(coltype, Native_UUID):
            # Whatever sampling refined the column to, the other side holds plain text
            return Text(digest=source)
        return super().as_digest_type(coltype, source)

    def _digest_length(self, value: str, coltype: ColType) -> str:
        return f"LENGTH({value})" if isinstance(coltype, Binary) else f"CHARACTER_LENGTH({value}, CODEUNITS32)"

    def digest_needs_client(self, value: str, coltype: ColType) -> Optional[str]:
        digest = getattr(coltype, "digest", None)
        if digest is None or not digest.client:
            return None
        n = BINARY_DIGEST_CHUNK if isinstance(coltype, Binary) else TEXT_DIGEST_CHUNK
        return f"{self._digest_length(value, coltype)} > {MAX_DIGEST_CHUNKS * n}"

    def normalize_digest(self, value: str, coltype: ColType) -> str:
        length = self._digest_length(value, coltype)
        is_binary = isinstance(coltype, Binary)
        n = BINARY_DIGEST_CHUNK if is_binary else TEXT_DIGEST_CHUNK
        start = f"k.i * {n} + 1"

        if is_binary:
            one_chunk = f"BX'{b'0|'.hex()}' || CAST(SUBSTRING({value}, 1, {n}, OCTETS) AS VARBINARY({n}))"
            lengths = f"LENGTH({value}), LENGTH({value})"
            chunk = (
                f"CAST(VARCHAR(k.i) || '|' AS VARBINARY(13))"
                f" || CAST(SUBSTRING({value}, {start}, {n}, OCTETS) AS VARBINARY({n}))"
            )
        else:
            # A character takes up to 4 bytes. A cast that would truncate fails, even in a branch
            # of a CASE that isn't taken, so the first chunk is cut like the others.
            one_chunk = f"'0|' || CAST(SUBSTRING({value}, 1, {n}, CODEUNITS32) AS VARCHAR({4 * n}))"
            lengths = f"LENGTH({value}, OCTETS), {length}"
            # Finding a character in a CLOB reads it from the start, which takes about 4 seconds
            # for each MB. ASCII text has one byte for each character, and cutting bytes takes
            # 0.6 seconds for each MB. Cutting bytes pads the last chunk, unless its length is given.
            # Db2 may evaluate it for a number past the end, where the length must still be valid.
            by_byte = f"SUBSTRING({value}, {start}, MAX(1, MIN({n}, l.octets - k.i * {n})), OCTETS)"
            by_character = f"SUBSTRING({value}, {start}, {n}, CODEUNITS32)"
            chunk = (
                f"VARCHAR(k.i) || '|' || CASE WHEN l.octets = l.len THEN CAST({by_byte} AS VARCHAR({4 * n}))"
                f" ELSE CAST({by_character} AS VARCHAR({4 * n})) END"
            )

        # Db2 has no series of numbers, so three digits in base 32 count the chunks. The first
        # digit is limited by the length, so a value of a few chunks doesn't count to 32,768.
        digit = "TABLE(VALUES " + ", ".join(f"({i})" for i in range(DIGEST_BASE)) + ")"
        last = f"(l.len - 1) / {n}"
        numbers = (
            f"LATERAL(SELECT a.n * {DIGEST_BASE ** 2} + b.n * {DIGEST_BASE} + c.n AS i"
            f" FROM {digit} AS a(n), {digit} AS b(n), {digit} AS c(n)"
            f" WHERE a.n <= {last} / {DIGEST_BASE ** 2})"
        )
        # The sum is over a derived table, because an aggregate can't mix an outer column with k.i.
        # LATERAL lets a derived table read a column of the outer query, and the lengths are in
        # their own, so that they're computed once for the value instead of once for each number.
        chunks = (
            f"(SELECT SUM(x.term) FROM LATERAL(VALUES ({lengths})) AS l(octets, len),"
            f" LATERAL(SELECT {self._md5_tail(chunk)} AS term FROM {numbers} AS k WHERE k.i <= {last}) AS x)"
        )

        if coltype.digest.client:
            too_long = "CAST(NULL AS VARCHAR(30))"
        else:
            message = (
                f"data-diff: a value over {MAX_DIGEST_CHUNKS * n} {'bytes' if is_binary else 'characters'}"
                " can not be hashed on Db2, so compare this table with the rowhash algorithm"
            )
            too_long = f"RAISE_ERROR('70001', '{message}')"
        return (
            f"CASE WHEN {value} IS NULL THEN NULL WHEN LENGTH({value}) = 0 THEN '0'"
            f" WHEN {length} <= {n} THEN VARCHAR({self._md5_tail(one_chunk)})"
            f" WHEN {length} <= {MAX_DIGEST_CHUNKS * n} THEN VARCHAR({chunks})"
            f" ELSE {too_long} END"
        )

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
            "CASE WHEN TYPENAME IN ('TIMESTAMP', 'TIME') THEN SCALE END, "
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

    def _refine_coltypes(
        self, table_path: DbPath, col_dict: Dict[str, ColType], where: Optional[str] = None, sample_size=64
    ):
        # A CLOB can't be a key, and sampling one over 32,672 bytes fails, so they aren't sampled
        lobs = {k: v for k, v in col_dict.items() if isinstance(v, Db2Lob)}
        for name in lobs:
            del col_dict[name]
        super()._refine_coltypes(table_path, col_dict, where, sample_size)
        col_dict.update(lobs)
        return col_dict

    def _query_cursor(self, c, sql_code: str):
        # ibm_db cuts one character off the end of a statement for each character outside the
        # Basic Multilingual Plane, such as an emoji in a key bound. Trailing spaces absorb that.
        sql_code += " " * sum(ord(char) > 0xFFFF for char in sql_code)
        try:
            return super()._query_cursor(c, sql_code)
        except Exception as e:  # ibm_db_dbi raises a plain Exception when a fetch fails
            # A failed fetch keeps its transaction open despite autocommit, and that transaction
            # keeps the table locked, which blocks DROP TABLE
            for release in (c.close, self.thread_local.conn.rollback):
                with suppress(Exception):  # The original error is the one to report
                    release()
            if "SQL0433N" in str(e):
                raise QueryError(
                    f"{e}\nDb2 can only hash text up to 32,672 bytes, and CLOB columns can hold more. "
                    "Remove the column from extra_columns, or compare it some other way."
                )
            raise QueryError(e)
