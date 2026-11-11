"""SAP Sybase ASE, through pyodbc and the FreeTDS ODBC driver (TDS 5.0).

Checksums must hash the same bytes as other databases, which hash UTF-8. ASE can't convert text to
UTF-8 on the server, so this dialect builds the bytes in SQL:

- cp850 text (varchar, char, text) goes through univarchar, where each non-ASCII character is
  replaced with Private Use Area characters U+E000 + b, one for each byte b of its UTF-8 encoding.
  As UTF-16LE, ASCII is ``xx 00`` and those characters are ``bb E0``, so removing every 00 and E0
  byte leaves the UTF-8 bytes. Replacing bytes directly can't work, because the 128 mappings form
  a cycle.
- Unicode text (univarchar, unichar, unitext) is converted one UTF-16 code unit at a time, which
  makes the SQL grow with the column length, so it has a limit.

The server's character set is cp850, and FreeTDS sends an empty character set when it logs in,
so SQL text must only contain cp850 characters. Other literals are sent as U&'' escapes.
"""

from contextlib import suppress
import logging
import time
from datetime import timezone
from typing import Any, ClassVar, Dict, List, Optional, Type

import attrs

from data_diff.abcs.database_types import (
    Binary,
    Boolean,
    ColType,
    Date,
    DbPath,
    DbTime,
    Decimal,
    DigestSource,
    Float,
    FractionalType,
    Integer,
    LongText,
    String_UUID,
    String_VaryingAlphanum,
    TemporalType,
    Text,
    Time,
    Timestamp,
)
from data_diff.databases.base import (
    CHECKSUM_HEXDIGITS,
    CHECKSUM_OFFSET,
    TIMESTAMP_PRECISION_POS,
    BaseDialect,
    Compiler,
    ConnectError,
    QueryError,
    QueryResult,
    ThreadedDatabase,
    import_helper,
)
from data_diff.queries.ast_classes import ConstantTable, CreateTable, DropTable, InsertToTable
from data_diff.queries.extras import Checksum, NormalizeAsString, RawForClient
from data_diff.schema import RawColumnInfo
from data_diff.utils import BINARY_DIGEST_CHUNK, DIGEST_HEXDIGITS, TEXT_DIGEST_CHUNK

logger = logging.getLogger("sybase")

# The longest string, varbinary or concatenation an ASE expression can hold. Longer ones are
# truncated without an error, so every path that could exceed it checks first.
MAX_EXPRESSION_BYTES = 16384
# univarchar expressions hold up to 8192 code units. A non-ASCII character becomes up to 3 of
# them, so cp850 text is converted in chunks of this many bytes.
CP850_CHUNK = 2730
# Unicode text outside cp850 is converted one code unit at a time, with about 1.6 KB of SQL for
# each. Longer values fail with an error, instead of exhausting the procedure cache. unitext
# always needs the row length check, which repeats the conversion, so it has a lower limit.
MAX_UNICODE_HASH_UNITS = 255
MAX_UNITEXT_HASH_UNITS = 128

# A large value is read in chunks through a LOB locator, one chunk for each number in
# master..spt_values, which has 0 to 1024. Reading a chunk takes longer the further into the value
# it is (1 MB of text takes 0.06 s, and 20 MB takes 81 s), so longer values are left to the client.
MAX_DIGEST_CHUNKS = 1025

UNICODE_STORAGE = ("univarchar", "unichar", "unitext")
PADDED_STORAGE = ("char", "nchar", "unichar")
LOB_STORAGE = ("text", "unitext")
# Storage that only a locator can read past 16384 bytes
LOCATOR_STORAGE = {"text": "text_locator", "image": "image_locator"}
# Storage that holds 1/300 second ticks, which ASE reports as truncated milliseconds
TICK_STORAGE = ("datetime", "time")

TIME_PRECISION_POS = len("12:24:35.")
MAX_BIGDATETIME = "9999-12-31 23:59:59.999999"
MAX_BIGTIME = "23:59:59.999999"


@import_helper("sybase")
def import_sybase():
    import pyodbc

    return pyodbc


# Column types that remember how ASE stores the value, which decides how it's normalized and
# hashed. attrs.evolve() keeps the subclass, so the storage survives precision adjustments.
@attrs.define(frozen=True)
class SybaseText(Text):
    storage: str = attrs.field(default="varchar", kw_only=True)
    # In characters, or None for text and unitext
    length: Optional[int] = attrs.field(default=None, kw_only=True)


# The same fields on the refined types. A shared base can't hold them, because attrs classes
# have slots, and two slotted bases with fields conflict.
@attrs.define(frozen=True)
class SybaseStringUUID(String_UUID):
    storage: str = attrs.field(default="varchar", kw_only=True)
    length: Optional[int] = attrs.field(default=None, kw_only=True)


@attrs.define(frozen=True)
class SybaseAlphanum(String_VaryingAlphanum):
    storage: str = attrs.field(default="varchar", kw_only=True)
    length: Optional[int] = attrs.field(default=None, kw_only=True)


@attrs.define(frozen=True)
class SybaseBinary(Binary):
    storage: str = attrs.field(default="varbinary", kw_only=True)


@attrs.define(frozen=True)
class SybaseTimestamp(Timestamp):
    storage: str = attrs.field(default="bigdatetime", kw_only=True)


@attrs.define(frozen=True)
class SybaseTime(Time):
    storage: str = attrs.field(default="bigtime", kw_only=True)


@attrs.define(frozen=True)
class SybaseFloat(Float):
    # Significant digits kept when converting to decimal, as PostgreSQL does: 15 for float, 6 for real
    significant_digits: int = attrs.field(default=15, kw_only=True)


@attrs.define(frozen=True)
class SybaseDecimal(Decimal):
    # The declared scale of numeric and decimal, or None for money and smallmoney, which convert
    # to varchar with 2 decimals. It has no storage field, because that would make it text.
    scale: Optional[int] = attrs.field(default=None, kw_only=True)


def _storage(coltype: Optional[ColType]) -> Optional[str]:
    return getattr(coltype, "storage", None)


def _error(message: str, detail: str) -> str:
    """SQL that fails with the message when it runs. The message must depend on a row value,
    or ASE may evaluate it while compiling and fail even if no row needs it."""
    return f"convert(int, 'data-diff: {message} (' + convert(varchar(12), {detail}) + ')')"


def _odbc_value(value) -> str:
    """A value for an ODBC connection string, such as a password with ; or = in it."""
    value = str(value)
    # Braces keep these characters from ending the value, and } is doubled inside them. Only
    # values that need them get them, because FreeTDS rejects a braced ClientCharset.
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _unicode_literal(s: str) -> str:
    escaped = "".join(
        ch
        if 32 <= ord(ch) < 127 and ch not in "'\\"
        else ("''" if ch == "'" else f"\\{ord(ch):04X}" if ord(ch) < 0x10000 else f"\\+{ord(ch):06X}")
        for ch in s
    )
    return f"U&'{escaped}'"


def _cp850_pua(x: str) -> str:
    """UTF-8 bytes of a cp850 varchar of up to CP850_CHUNK bytes, as varbinary."""
    # Converting to univarchar stops at the first NUL, and removing 00 bytes would drop it too,
    # so NUL is escaped first: 01 becomes 01 03, then 00 becomes 01 02. Every 01 then starts a
    # pair, and the UTF-8 bytes added below never contain 01, so the pairs decode back exactly.
    escaped = f"str_replace(str_replace({x}, char(1), char(1) + char(3)), char(0), char(1) + char(2))"
    expr = f"convert(univarchar(8192), {escaped})"
    for byte in range(128, 256):
        char = bytes([byte]).decode("cp850")
        pua = "+".join(f"to_unichar({0xE000 + b})" for b in char.encode("utf-8"))
        expr = f"str_replace({expr}, to_unichar({ord(char)}), {pua})"
    stripped = f"str_replace(str_replace(convert(varbinary(16384), {expr}), 0x00, null), 0xe0, null)"
    return f"str_replace(str_replace({stripped}, 0x0102, 0x00), 0x0103, 0x01)"


def _utf16_unit(b: str, i: int) -> str:
    # convert() reads two bytes of varbinary as little-endian, which is one UTF-16LE code unit.
    # Past the end of the value, substring() is NULL, and so is the unit.
    return f"convert(int, substring({b}, {2 * i - 1}, 2))"


def _utf8_hex_of_unit(b: str, i: int) -> str:
    c = _utf16_unit(b, i)
    n = _utf16_unit(b, i + 1)
    code_point = f"(65536 + ({c} - 55296) * 1024 + ({n} - 56320))"
    return (
        f"case when {c} < 128 then right(inttohex({c}), 2)"
        f" when {c} < 2048 then right(inttohex(49280 + ({c} / 64) * 256 + {c} % 64), 4)"
        # A high surrogate encodes the whole pair, and the low surrogate after it adds nothing
        f" when {c} between 55296 and 56319 then"
        f" right(inttohex(61568 + ({code_point} / 262144) * 256 + ({code_point} / 4096) % 64), 4)"
        f" + right(inttohex(32896 + (({code_point} / 64) % 64) * 256 + {code_point} % 64), 4)"
        f" when {c} between 56320 and 57343 then null"
        f" else right(inttohex(14712960 + ({c} / 4096) * 65536 + (({c} / 64) % 64) * 256 + {c} % 64), 6) end"
    )


@attrs.define(frozen=False)
class Dialect(BaseDialect):
    name = "Sybase"
    # Literals round to the precision of the column: '05.1255' is stored as .126 in a datetime,
    # and 29.999 seconds becomes the same minute in a smalldatetime. Casts of numbers truncate,
    # but normalize_number always rounds explicitly.
    ROUNDS_ON_PREC_LOSS = True
    SUPPORTS_PRIMARY_KEY: ClassVar[bool] = True
    SUPPORTS_INDEXES = True
    TYPE_CLASSES = {
        # Timestamps
        "datetime": SybaseTimestamp,
        "smalldatetime": SybaseTimestamp,
        "bigdatetime": SybaseTimestamp,
        "date": Date,
        "time": SybaseTime,
        "bigtime": SybaseTime,
        # Numbers
        "float": SybaseFloat,
        "real": SybaseFloat,
        "numeric": Decimal,
        "decimal": Decimal,
        "money": Decimal,
        "smallmoney": Decimal,
        # Integers
        "int": Integer,
        "bigint": Integer,
        "smallint": Integer,
        "tinyint": Integer,
        "uint": Integer,
        "ubigint": Integer,
        "usmallint": Integer,
        # Text in the server's character set
        "varchar": SybaseText,
        "char": SybaseText,
        "nvarchar": SybaseText,
        "nchar": SybaseText,
        "sysname": SybaseText,
        "longsysname": SybaseText,
        "text": SybaseText,
        # Text in UTF-16
        "univarchar": SybaseText,
        "unichar": SybaseText,
        "unitext": SybaseText,
        # Bytes. timestamp is a row version, which a copy of the row doesn't keep.
        "binary": SybaseBinary,
        "varbinary": SybaseBinary,
        "image": SybaseBinary,
        # Bool
        "bit": Boolean,
    }

    def quote(self, s: str) -> str:
        # Double quotes make a string unless quoted_identifier is on, and brackets always work,
        # except on temporary tables
        return s if s.startswith("#") else f"[{s}]"

    def parse_type(self, table_path: DbPath, info: RawColumnInfo) -> ColType:
        cls = self.TYPE_CLASSES.get(info.data_type)
        if cls is SybaseText:
            # numeric_precision holds the length in characters, see select_table_schema()
            return SybaseText(storage=info.data_type, length=info.numeric_precision)
        if cls is SybaseBinary:
            return SybaseBinary(storage=info.data_type)
        if cls in (SybaseTimestamp, SybaseTime):
            # ASE shows and converts datetime and time ticks by truncating: tick 299 is .996667, and
            # ASE says .996, so that's what a copy made through ASE holds.
            rounds = info.data_type not in TICK_STORAGE and self.ROUNDS_ON_PREC_LOSS
            return cls(precision=info.datetime_precision, rounds=rounds, storage=info.data_type)
        if cls is SybaseFloat:
            bits = info.numeric_precision
            return SybaseFloat(
                precision=self._convert_db_precision_to_digits(bits), significant_digits=15 if bits > 24 else 6
            )
        if cls is Decimal and info.numeric_scale == 0 and info.data_type in ("numeric", "decimal"):
            # Identity columns are numeric(n, 0), and only integers can be keys
            return Integer()
        coltype = super().parse_type(table_path, info)
        if cls is Decimal and info.data_type in ("numeric", "decimal"):
            return SybaseDecimal(precision=coltype.precision, scale=info.numeric_scale)
        return coltype

    def _convert_db_precision_to_digits(self, p: int) -> int:
        # Same reduction as PostgreSQL, so a real and a float holding the same number compare equal
        return max(super()._convert_db_precision_to_digits(p) - 2, 0)

    def set_timezone_to_utc(self) -> str:
        raise NotImplementedError("Sybase ASE does not support a session time zone.")

    def current_timestamp(self) -> str:
        return "getdate()"

    def current_database(self) -> str:
        return "db_name()"

    def current_schema(self) -> str:
        return "user_name()"

    def random(self) -> str:
        # rand() is evaluated once per query, and rand2() once per row
        return "rand2()"

    def to_string(self, s: str) -> str:
        # Only for values in the server's character set. Unicode text has its own path in
        # normalize_value_by_type(), because converting it to varchar replaces characters with '?'.
        return f"convert(varchar({MAX_EXPRESSION_BYTES}), {s})"

    def to_comparable(self, value: str, coltype: ColType) -> str:
        # text and unitext can't be compared with =
        if _storage(coltype) in LOB_STORAGE:
            return self._normalize_text(value, coltype)
        return value

    def is_distinct_from(self, a: str, b: str) -> str:
        return f"(({a}<>{b} OR {a} IS NULL OR {b} IS NULL) AND NOT({a} IS NULL AND {b} IS NULL))"

    def type_repr(self, t) -> str:
        try:
            return {
                bool: "bit",
                # Rows are limited to about 4,000 bytes, and text is stored outside the row
                str: "varchar(1000)",
                # Materialized rows can be longer than a page, and unitext is stored outside the row.
                # FreeTDS returns it as UTF-16LE bytes, like image.
                LongText: "unitext",
                float: "float",
            }[t]
        except (KeyError, TypeError):
            pass
        if getattr(t, "__name__", None) == "datetime":
            return "bigdatetime"
        return super().type_repr(t)

    def explain_as_text(self, query: str) -> str:
        raise NotImplementedError("Sybase ASE explains a query with set showplan, not as a result set.")

    def optimizer_hints(self, hints: str) -> str:
        return ""

    def limit_select(
        self,
        select_query: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        has_order_by: Optional[bool] = None,
    ) -> str:
        if offset:
            raise NotImplementedError("No support for OFFSET in query")
        # ASE has no LIMIT, and doesn't allow ORDER BY in a derived table, so TOP goes in the select itself
        for prefix in ("SELECT DISTINCT ", "SELECT "):
            if select_query.startswith(prefix):
                return f"{prefix}TOP {limit} {select_query[len(prefix):]}"
        return f"SELECT TOP {limit} * FROM ({select_query}) AS limited_select"

    def _constant_value(self, v):
        if isinstance(v, str):
            if not all(32 <= ord(ch) < 127 for ch in v):
                # Characters outside cp850 can't travel in the SQL text
                return _unicode_literal(v)
            return "'" + v.replace("'", "''") + "'"
        return super()._constant_value(v)

    def constant_values(self, rows) -> str:
        # ASE has no VALUES list of rows
        return " UNION ALL ".join("SELECT " + ", ".join(self._constant_value(v) for v in row) for row in rows)

    def timestamp_value(self, t: DbTime) -> str:
        # Columns have no time zone, so a value with one must be converted, not just stripped of it
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return f"'{t.isoformat(sep=' ')}'"

    def _path_for_object_id(self, c: Compiler, path) -> str:
        # object_id() doesn't accept brackets
        return ".".join(path.path) if hasattr(path, "path") else self.compile(c, path)

    def render_createtable(self, c: Compiler, elem: CreateTable) -> str:
        path = self.compile(c, elem.path)
        if elem.source_table:
            statement = f"SELECT * INTO {path} FROM ({self.compile(c, elem.source_table)}) AS source"
        else:
            # ASE makes a column NOT NULL unless it says otherwise, and other databases do the opposite
            keys = set(elem.primary_keys or ())
            columns = ", ".join(
                f"{self.quote(k)} {self.type_repr(v)}{self._nullability(k in keys, v)}"
                for k, v in elem.path.schema.items()
            )
            pks = f", PRIMARY KEY ({', '.join(elem.primary_keys)})" if elem.primary_keys else ""
            statement = f"CREATE TABLE {path}({columns}{pks})"
        if elem.if_not_exists:
            # ASE compiles the whole batch first, which fails on a table that exists unless the
            # CREATE is compiled only when it runs
            escaped = statement.replace("'", "''")
            return f"IF object_id('{self._path_for_object_id(c, elem.path)}') IS NULL EXEC('{escaped}')"
        return statement

    def _nullability(self, is_key: bool, type_) -> str:
        # bit can't be NULL, and ASE refuses to be told it can, or to default to it when the
        # database allows nulls by default
        if type_ is bool or (isinstance(type_, str) and type_.lower() == "bit"):
            return " NOT NULL"
        return " NOT NULL" if is_key else " NULL"

    def render_droptable(self, c: Compiler, elem: DropTable) -> str:
        statement = f"DROP TABLE {self.compile(c, elem.path)}"
        if elem.if_exists:
            return f"IF object_id('{self._path_for_object_id(c, elem.path)}') IS NOT NULL {statement}"
        return statement

    def render_inserttotable(self, c: Compiler, elem: InsertToTable) -> str:
        if not isinstance(elem.expr, ConstantTable):
            return super().render_inserttotable(c, elem)
        # One INSERT per row, in one batch, because ASE inserts only one VALUES row at a time
        columns = "(%s)" % ", ".join(map(self.quote, elem.columns)) if elem.columns is not None else ""
        path = self.compile(c, elem.path)
        return "\n".join(
            f"INSERT INTO {path}{columns} VALUES ({', '.join(self._constant_value(v) for v in row)})"
            for row in elem.expr.rows
        )

    # Normalization

    def _normalize_text(self, value: str, coltype: ColType) -> str:
        storage = _storage(coltype)
        if storage in LOB_STORAGE:
            unicode = storage == "unitext"
            target = "univarchar(8192)" if unicode else f"varchar({MAX_EXPRESSION_BYTES})"
            converted = f"convert({target}, {value})"
            # Converting text drops trailing spaces, so they're added back
            space = "convert(univarchar(1), ' ')" if unicode else "' '"
            char_bytes = 2 if unicode else 1
            restored = (
                f"{converted} + replicate({space}, (datalength({value}) - datalength({converted})) / {char_bytes})"
            )
            # A longer value would be truncated without an error
            return (
                f"case when datalength({value}) > {MAX_EXPRESSION_BYTES} then"
                f" convert({target}, {_error('text over 16384 bytes can not be compared on Sybase ASE', f'datalength({value})')})"
                f" else {restored} end"
            )
        if storage in PADDED_STORAGE:
            # char NOT NULL is padded with spaces, and other databases compare char without them.
            # rtrim() returns NULL for a value of only spaces, which would hash the same as NULL.
            space = "convert(univarchar(1), ' ')" if storage == "unichar" else "' '"
            return f"case when {value} is null then null else isnull(rtrim({value}), {space}) end"
        return value

    def normalize_value_by_type(self, value: str, coltype: ColType) -> str:
        if getattr(coltype, "digest", None) is not None:
            return self.normalize_digest(value, coltype)
        if _storage(coltype) in UNICODE_STORAGE + PADDED_STORAGE + LOB_STORAGE or isinstance(coltype, SybaseText):
            text = self._normalize_text(value, coltype)
            if isinstance(coltype, String_UUID):
                text = self.normalize_uuid(text, coltype)
                if coltype.ignore_case:
                    text = f"lower({text})"
            return text
        if isinstance(coltype, Integer):
            return f"convert(varchar(40), {value})"
        return super().normalize_value_by_type(value, coltype)

    def normalize_uuid(self, value: str, coltype) -> str:
        if isinstance(coltype, String_UUID):
            return f"ltrim(rtrim({value}))"
        return super().normalize_uuid(value, coltype)

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return f"convert(varchar(1), {value})"

    def _exact_bigdatetime(self, value: str, coltype: TemporalType) -> str:
        storage = _storage(coltype)
        target = "bigtime" if isinstance(coltype, Time) else "bigdatetime"
        if storage not in TICK_STORAGE:
            return value if storage in ("bigdatetime", "bigtime") else f"convert({target}, {value})"
        # datetime and time store 1/300 second ticks, which ASE reports as truncated milliseconds:
        # tick 299 is .996667, and ASE says .996. Recover the tick, and use its exact value, which
        # is also what SQL Server's datetime holds.
        ms = f"datepart(ms, {value})"
        micros = f"((({ms} * 3 + 5) / 10) * 10000 + 1) / 3"
        return f"dateadd(us, {micros}, convert({target}, dateadd(ms, -{ms}, {value})))"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if isinstance(coltype, Date):
            return f"convert(char(10), {value}, 140)"

        is_time = isinstance(coltype, Time)
        exact = self._exact_bigdatetime(value, coltype)
        precision = min(coltype.precision, 6)
        if coltype.rounds and precision < 6:
            half = 5 * 10 ** (5 - precision)
            # Rounding up the last moment, often used to mean "no end date", would overflow
            limit = f"convert(bigtime, '{MAX_BIGTIME}')" if is_time else f"convert(bigdatetime, '{MAX_BIGDATETIME}')"
            exact = (
                f"case when {exact} < dateadd(us, -{half}, {limit}) then dateadd(us, {half}, {exact}) else {exact} end"
            )

        if is_time:
            rendered = f"convert(varchar(15), {exact}, 37)"
            pos = TIME_PRECISION_POS
        else:
            rendered = f"convert(varchar(26), {exact}, 140)"
            pos = TIMESTAMP_PRECISION_POS
        if precision == 6:
            return rendered
        return f"left({rendered}, {pos + precision}) + '{'0' * (6 - precision)}'"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        precision = coltype.precision
        if isinstance(coltype, Float):
            return self._normalize_float(value, coltype)
        if getattr(coltype, "scale", None) == precision:
            # numeric or decimal with the decimals compared, which converting to varchar shows
            return f"convert(varchar(50), {value})"
        # Casts truncate, so round first. money converts to numeric with all 4 of its decimals.
        return f"convert(varchar(50), convert(numeric(38, {precision}), round({value}, {precision})))"

    def _normalize_float(self, value: str, coltype: Float) -> str:
        # PostgreSQL converts a float to decimal through its first 15 significant digits (6 for
        # real), then rounds that to the precision, which is two roundings: -9.97154999999 becomes
        # -9.97155, then -9.9716. ASE converts through 17 digits, so this rounds the exact binary
        # digits from str(), which gives up to 16 decimals, at the same places.
        precision = coltype.precision
        significant = getattr(coltype, "significant_digits", 15)
        f = f"convert(float, {value})"
        significant_places = f"{significant - 1} - floor(log10(abs({f})))"

        def fixed(digits: str) -> str:
            rounded = f"round(round({digits}, {significant_places}), {precision})"
            return f"convert(varchar(60), convert(numeric(38, {precision}), {rounded}))"

        small = fixed(f"convert(numeric(38, 16), ltrim(str({f}, 40, 16)))")
        large = fixed(f"convert(numeric(38, 0), ltrim(str({f}, 40, 0)))")
        zero = f"convert(varchar(60), convert(numeric(38, {precision}), 0))"
        whole_digits = 38 - precision
        # Values too large for any decimal keep a distinct form, like Db2's
        return (
            f"case when {f} = 0 then {zero}"
            f" when abs({f}) < 1e22 then {small}"
            f" when abs({f}) < 1e{whole_digits} then {large}"
            f" else convert(varchar(60), {f}) end"
        )

    # Checksums

    def _utf8(self, normalized: str, coltype: Optional[ColType], raw: Optional[str] = None) -> str:
        """The UTF-8 bytes of a normalized value, as varbinary. NULL stays NULL."""
        storage = _storage(coltype)
        if getattr(coltype, "digest", None) is not None:
            # A digest is a number
            storage = None
        if storage in UNICODE_STORAGE:
            return self._unicode_utf8(normalized, coltype, raw if storage == "unitext" else None)
        if storage is not None and not isinstance(coltype, TemporalType):
            return self._cp850_utf8(normalized, coltype)
        # Numbers, dates and booleans normalize to ASCII
        return f"convert(varbinary({MAX_EXPRESSION_BYTES}), {normalized})"

    def _cp850_utf8(self, x: str, coltype: ColType) -> str:
        length = getattr(coltype, "length", None) or MAX_EXPRESSION_BYTES
        length = min(length, MAX_EXPRESSION_BYTES)
        chunks = " + ".join(
            _cp850_pua(f"substring({x}, {start + 1}, {CP850_CHUNK})") for start in range(0, length, CP850_CHUNK)
        )
        high_byte = "'%[' + char(128) + '-' + char(255) + ']%'"
        # Most text is ASCII, and its bytes are already UTF-8
        return f"case when patindex({high_byte}, {x}) = 0 then convert(varbinary({MAX_EXPRESSION_BYTES}), {x}) else {chunks} end"

    def _unicode_utf8(self, u: str, coltype: ColType, lob: Optional[str] = None) -> str:
        # unitext has no declared length
        length = getattr(coltype, "length", None) or 2**31
        limit = MAX_UNITEXT_HASH_UNITS if lob else MAX_UNICODE_HASH_UNITS
        units = min(length, limit)
        # The conversion reads its input about 24 times for each character, which for unitext
        # would repeat the whole normalized expression. The column itself is shorter, but
        # converting it drops trailing spaces, so they're added back as hex.
        source = f"convert(univarchar({units}), {lob})" if lob else u
        b = f"convert(varbinary({2 * units}), {source})"
        hex_digits = " + ".join(_utf8_hex_of_unit(b, i) for i in range(1, units + 1))
        if lob:
            hex_digits += f" + replicate('20', (datalength({lob}) - datalength({source})) / 2)"
        converted = f"strtobin({hex_digits})"
        if length > limit:
            too_long = _error(
                f"Unicode text over {limit} characters, with characters outside cp850,"
                " can not be hashed on Sybase ASE",
                f"char_length({u})",
            )
            converted = (
                f"case when datalength({u}) > {2 * limit} then convert(varbinary(1), {too_long})"
                f" else {converted} end"
            )
        # Most Unicode text also fits in cp850, which has a path without a length limit. Converting
        # to varchar replaces other characters with '?', so the value must convert back unchanged.
        # = ignores trailing spaces, and the lengths don't.
        as_cp850, fits_cp850, cp850_type = self._as_cp850(u, length)
        return f"case when {fits_cp850} then {self._cp850_utf8(as_cp850, cp850_type)} else {converted} end"

    def _as_cp850(self, u: str, length: int):
        """Unicode text as cp850, the condition that tells whether it converted without loss,
        and the type of the result."""
        cp850_bytes = min(length, MAX_EXPRESSION_BYTES // 2)
        as_cp850 = f"convert(varchar({cp850_bytes}), {u})"
        fits_cp850 = (
            f"convert(univarchar({cp850_bytes}), {as_cp850}) = {u}"
            f" and datalength(convert(univarchar({cp850_bytes}), {as_cp850})) = datalength({u})"
        )
        return as_cp850, fits_cp850, SybaseText(storage="varchar", length=cp850_bytes)

    def render_rowhash(self, c: Compiler, elem: Checksum) -> str:
        pieces = []
        text_lengths = []
        text_pieces = []
        # The most bytes a row can have, from the declared lengths, or None if one is unknown
        declared_bound = 0
        for expr in elem.exprs:
            coltype = expr.expr_type or expr.expr.type if isinstance(expr, NormalizeAsString) else None
            normalized = self.compile(c, expr)
            raw = self.compile(c, expr.expr) if isinstance(expr, NormalizeAsString) else None
            piece = self._utf8(normalized, coltype, raw)
            is_text = (
                _storage(coltype) is not None
                and not isinstance(coltype, TemporalType)
                and getattr(coltype, "digest", None) is None
            )
            if is_text:
                # UTF-8 takes up to 3 bytes for each cp850 byte, or for each UTF-16 code unit
                factor = 2 if _storage(coltype) in UNICODE_STORAGE else 3
                text_lengths.append(f"isnull(datalength({normalized}), 0) * {factor}")
                length = getattr(coltype, "length", None)
                declared_bound = None if length is None or declared_bound is None else declared_bound + 3 * length
            text_pieces.append(is_text)
            if declared_bound is not None:
                declared_bound += 64
            pieces.append(piece)

        if len(pieces) > 1:
            # '<null>' and '|', as other databases concatenate them
            pieces = [f"isnull({p}, 0x3c6e756c6c3e)" for p in pieces]
            if self._prevent_overflow_when_concat:
                # As BaseDialect.render_concat: the MD5 of each value, in hex, instead of the value.
                # Each value must fit the limit by itself, which only text can exceed.
                hashed = []
                for p, is_text in zip(pieces, text_pieces):
                    if is_text:
                        too_long = _error(
                            "a value over 16384 bytes can not be hashed on Sybase ASE", f"datalength({p})"
                        )
                        p = f"case when datalength({p}) < {MAX_EXPRESSION_BYTES} then {p} else convert(varbinary(1), {too_long}) end"
                    hashed.append(f"convert(varbinary(32), hash({p}, 'md5'))")
                pieces, text_lengths = hashed, []
            joined = " + 0x7c + ".join(pieces)
        else:
            (joined,) = pieces

        checksum = self.md5_as_int(joined)
        if text_lengths and (declared_bound is None or declared_bound >= MAX_EXPRESSION_BYTES):
            # A concatenation over 16384 bytes is truncated without an error. The sum of upper
            # bounds is cheap, and only rows that could be that long pay for measuring.
            upper_bound = " + ".join(text_lengths) + f" + {64 * len(pieces)}"
            # Each copy of the concatenation costs procedure cache, so it appears only twice
            too_long = _error("a row over 16384 bytes of text can not be hashed on Sybase ASE", upper_bound)
            checksum = (
                f"case when {upper_bound} < {MAX_EXPRESSION_BYTES} or datalength({joined}) < {MAX_EXPRESSION_BYTES}"
                f" then {checksum} else convert(bigint, {too_long}) end"
            )
        return checksum

    # Large values, compared by digest

    def needs_digest(self, coltype: ColType) -> bool:
        return isinstance(coltype, Binary) or _storage(coltype) in LOB_STORAGE

    def as_digest_type(self, coltype: ColType, source: DigestSource) -> ColType:
        storage = _storage(coltype)
        if isinstance(coltype, SybaseBinary):
            return attrs.evolve(coltype, digest=source)
        if storage is not None and isinstance(coltype, (SybaseText, SybaseStringUUID, SybaseAlphanum)):
            length = coltype.length
            if storage in UNICODE_STORAGE and storage != "unitext" and (length or 0) > TEXT_DIGEST_CHUNK:
                raise NotImplementedError(
                    f"Sybase ASE can't compare a {storage}({length}) column with a large value. "
                    f"Columns of up to {TEXT_DIGEST_CHUNK} characters can be."
                )
            # Whatever sampling refined the column to, the other side holds plain text
            return SybaseText(storage=storage, length=length, digest=source)
        return super().as_digest_type(coltype, source)

    @staticmethod
    def _digest_term(index: str, chunk: str) -> str:
        return f"hextobigint(right(hash({index} + {chunk}, 'md5'), {DIGEST_HEXDIGITS}))"

    @staticmethod
    def _digest_index(i: int) -> str:
        return "0x" + f"{i}|".encode("ascii").hex()

    @staticmethod
    def _padded(chunk: str, length: str) -> str:
        """A varchar chunk with the trailing spaces that converting it dropped. A chunk of only
        spaces converts to one space."""
        chunk = f"isnull({chunk}, ' ')"
        return f"{chunk} + replicate(' ', {length} - datalength({chunk}))"

    def _digest_limit(self, coltype: ColType) -> Optional[int]:
        """The longest value the server hashes, in characters or bytes. None when the type itself
        is short enough."""
        storage = _storage(coltype)
        if storage == "text":
            return MAX_DIGEST_CHUNKS * TEXT_DIGEST_CHUNK
        if storage == "image":
            return MAX_DIGEST_CHUNKS * BINARY_DIGEST_CHUNK
        if storage == "unitext":
            return TEXT_DIGEST_CHUNK
        return None

    def _unitext_left_to_client(self, value: str, coltype: ColType) -> str:
        """1 for a unitext value the server can't hash: one over a chunk, or one with characters
        outside cp850 that's over the limit of the conversion to UTF-8."""
        plain = SybaseText(storage="unitext")
        _, fits_cp850, _ = self._as_cp850(self._normalize_text(value, plain), TEXT_DIGEST_CHUNK)
        return (
            f"case when char_length({value}) > {TEXT_DIGEST_CHUNK} then 1 when {fits_cp850} then 0"
            f" when char_length({value}) > {MAX_UNITEXT_HASH_UNITS} then 1 else 0 end"
        )

    def digest_needs_client(self, value: str, coltype: ColType) -> Optional[str]:
        digest = getattr(coltype, "digest", None)
        limit = self._digest_limit(coltype)
        if digest is None or not digest.client or limit is None:
            return None
        storage = _storage(coltype)
        if storage == "unitext":
            return f"({self._unitext_left_to_client(value, coltype)}) = 1"
        length = "datalength" if storage == "image" else "char_length"
        return f"{length}({value}) > {limit}"

    def render_rawforclient(self, c: Compiler, elem: RawForClient) -> str:
        # A CASE over a text or image column returns an empty value where it should return the
        # value or NULL, so the column is selected as it is, for every row
        value = self.compile(c, elem.expr)
        return value if self.digest_needs_client(value, elem.expr.type) else "NULL"

    def decode_raw(self, value: Any, coltype: ColType) -> Any:
        if _storage(coltype) == "unitext":
            # FreeTDS returns unitext as its UTF-16LE bytes
            return bytes(value).decode("utf-16-le")
        return super().decode_raw(value, coltype)

    def normalize_digest(self, value: str, coltype: ColType) -> str:
        source: DigestSource = coltype.digest
        storage = _storage(coltype)
        limit = self._digest_limit(coltype)
        first = self._digest_index(0)

        def as_string(total: str) -> str:
            return f"convert(varchar(30), {total})"

        def too_long(what: str, length: str) -> str:
            if source.client:
                # The client hashes it. See digest_needs_client().
                return "null"
            message = f"{what} can not be hashed on Sybase ASE, so compare this table with the rowhash algorithm"
            return f"convert(varchar(30), {_error(message, length)})"

        if storage == "unitext":
            plain = SybaseText(storage="unitext")
            utf8 = self._unicode_utf8(self._normalize_text(value, plain), plain, value)
            if source.client:
                guard = f"when ({self._unitext_left_to_client(value, coltype)}) = 1 then null"
            else:
                over = too_long(f"unitext over {limit} characters", f"char_length({value})")
                guard = f"when char_length({value}) > {limit} then {over}"
            return f"case when {value} is null then null {guard} else {as_string(self._digest_term(first, utf8))} end"

        if storage in LOCATOR_STORAGE:
            return self._normalize_lob_digest(value, coltype, as_string, too_long)

        if isinstance(coltype, Binary):
            # binary and varbinary hold up to 16384 bytes, which is two chunks
            n = BINARY_DIGEST_CHUNK
            terms = [self._digest_term(first, f"substring({value}, 1, {n})")]
            second = self._digest_term(self._digest_index(1), f"substring({value}, {n + 1}, {n})")
            terms.append(f"case when datalength({value}) > {n} then {second} else 0 end")
            return f"case when {value} is null then null else {as_string(' + '.join(terms))} end"

        # Text of a declared length, in up to four chunks
        n = TEXT_DIGEST_CHUNK
        length = getattr(coltype, "length", None) or MAX_EXPRESSION_BYTES
        if storage in UNICODE_STORAGE:
            text = self._normalize_text(value, coltype)
            return f"case when {value} is null then null else {as_string(self._digest_term(first, self._unicode_utf8(text, coltype)))} end"
        text = self._normalize_text(value, coltype)
        chunk_type = SybaseText(storage="varchar", length=n)
        terms = []
        for i in range(0, -(-min(length, MAX_EXPRESSION_BYTES) // n)):
            chunk_length = (
                f"case when char_length({text}) >= {(i + 1) * n} then {n} else char_length({text}) - {i * n} end"
            )
            chunk = self._padded(f"substring({text}, {i * n + 1}, {n})", chunk_length)
            term = self._digest_term(self._digest_index(i), self._cp850_utf8(chunk, chunk_type))
            terms.append(term if i == 0 else f"case when char_length({text}) > {i * n} then {term} else 0 end")
        return f"case when {value} is null then null else {as_string(' + '.join(terms))} end"

    def _normalize_lob_digest(self, value: str, coltype: ColType, as_string, too_long) -> str:
        source: DigestSource = coltype.digest
        storage = _storage(coltype)
        limit = self._digest_limit(coltype)
        is_text = storage == "text"
        n = TEXT_DIGEST_CHUNK if is_text else BINARY_DIGEST_CHUNK
        chunk_sql_type = f"varchar({n})" if is_text else f"varbinary({n})"
        chunk_type = SybaseText(storage="varchar", length=n)
        length_of = "char_length" if is_text else "datalength"
        first = self._digest_index(0)

        def to_bytes(chunk: str, length: str) -> str:
            # Converting a chunk of text drops its trailing spaces. Bytes keep their zeros.
            return self._cp850_utf8(self._padded(chunk, length), chunk_type) if is_text else chunk

        one_chunk = self._digest_term(first, to_bytes(f"convert({chunk_sql_type}, {value})", f"{length_of}({value})"))

        # substring() only reads a text or image column through a locator, and ASE doesn't allow
        # an aggregate over a column of the outer query, so the chunks come from a join of the
        # table with itself on its key.
        if not value.startswith("["):
            raise NotImplementedError(f"Sybase ASE can only hash a large value from its own column, not {value}")
        table = ".".join(map(self.quote, source.table_path))
        inner = f"y.{value}"
        on_key = " and ".join(f"y.{self.quote(k)} = {table}.{self.quote(k)}" for k in source.key_columns)
        locator = f"create_locator({LOCATOR_STORAGE[storage]}, {inner})"
        chunk = f"convert({chunk_sql_type}, substring({locator}, k.number * {n} + 1, {n}))"
        chunk_length = (
            f"case when (k.number + 1) * {n} <= {length_of}({inner}) then {n}"
            f" else {length_of}({inner}) - k.number * {n} end"
        )
        index = "convert(varbinary(13), convert(varchar(12), k.number) + '|')"
        chunks = (
            f"(select sum({self._digest_term(index, to_bytes(chunk, chunk_length))})"
            f" from master..spt_values k, {table} y"
            f" where {on_key} and k.type = 'P' and k.number <= ({length_of}({inner}) - 1) / {n})"
        )
        what = f"{storage} over {limit} {'characters' if is_text else 'bytes'}"
        return (
            f"case when {value} is null then null"
            f" when {length_of}({value}) <= {n} then {as_string(one_chunk)}"
            f" when {length_of}({value}) <= {limit} then {as_string(chunks)}"
            f" else {too_long(what, f'{length_of}({value})')} end"
        )

    def md5_as_int(self, s: str) -> str:
        return f"hextobigint(right(hash({s}, 'md5'), {CHECKSUM_HEXDIGITS})) - {CHECKSUM_OFFSET}"

    def md5_as_hex(self, s: str) -> str:
        return f"hash({s}, 'md5')"


@attrs.define(frozen=False, init=False, kw_only=True)
class Sybase(ThreadedDatabase):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = "sybase://<user>:<password>@<host>:<port>/<database>/<owner>"
    CONNECT_URI_PARAMS = ["database", "schema?"]

    default_database: str
    _conn_str: str
    _pyodbc: Any

    def __init__(
        self, host, user, password, *, database, schema=None, port=5000, odbc_driver="FreeTDS", thread_count, **kw
    ) -> None:
        super().__init__(thread_count=thread_count)

        # TDS 5.0 is the Sybase protocol. Any other parameter is passed through to the driver.
        args: Dict[str, Any] = dict(
            DRIVER=odbc_driver,
            SERVER=host,
            PORT=port,
            UID=user,
            PWD=password,
            DATABASE=database,
            TDS_Version="5.0",
            ClientCharset="UTF-8",
        )
        args.update(kw)
        self._conn_str = ";".join(f"{k}={_odbc_value(v)}" for k, v in args.items() if v is not None)
        self.default_database = database
        self.default_schema = schema or "dbo"
        self._pyodbc = None

    @property
    def is_autocommit(self) -> bool:
        return True

    def create_connection(self):
        self._pyodbc = import_sybase()
        try:
            connection = self._pyodbc.connect(self._conn_str, autocommit=True)
        except self._pyodbc.Error as error:
            raise ConnectError(*error.args) from error
        cursor = connection.cursor()
        # Unchained mode: each statement commits, and DDL isn't held in an open transaction.
        # textsize: raw text values are otherwise cut at 32 KB.
        cursor.execute("set chained off set textsize 2147483647")
        # Hashing converts cp850 to UTF-8 with str_replace(), which follows the sort order. A sort
        # order that ignores case or accents would replace the wrong characters, so it's refused.
        cursor.execute(
            "select cs.name, case when 'A' <> 'a' and str_replace(char(130), char(144), 'x') = char(130)"
            " and str_replace(to_unichar(233), to_unichar(201), 'x') = to_unichar(233) then 1 else 0 end"
            " from master..syscharsets cs where cs.type < 2000"
            " and cs.id = (select value from master..sysconfigures where config = 131)"
        )
        charset, binary = cursor.fetchone()
        cursor.close()
        if charset != "cp850" or not binary:
            connection.close()
            raise ConnectError(
                f"data-diff supports Sybase ASE servers with the cp850 character set and a binary sort order, "
                f"and this one has {charset}{'' if binary else ' with a sort order that ignores case or accents'}."
            )
        return connection

    def _run_on_connection(self, fn):
        """Runs fn(pyodbc connection) in the worker thread that owns the connection."""

        def run():
            if self._init_error:
                raise self._init_error
            return fn(self.thread_local.conn)

        return self._queue.submit(run).result()

    def select_table_schema(self, path: DbPath) -> str:
        """Provide SQL for selecting the table schema as (name, type, date_prec, num_prec, num_scale)"""
        database, owner, name = self._normalize_table_path(path)
        db = self.dialect.quote(database)
        # ASE has no information_schema. For text, the precision column holds the length in
        # characters, and for float, its binary precision.
        return (
            "SELECT c.name, t.name, "
            "CASE t.name WHEN 'datetime' THEN 3 WHEN 'time' THEN 3 WHEN 'smalldatetime' THEN 0 WHEN 'date' THEN 0 "
            "WHEN 'bigdatetime' THEN 6 WHEN 'bigtime' THEN 6 END, "
            "CASE WHEN t.name IN ('float', 'real') THEN (CASE WHEN c.length = 4 THEN 24 ELSE 53 END) "
            "WHEN t.name IN ('univarchar', 'unichar') THEN c.length / @@unicharsize "
            "WHEN t.name IN ('text', 'unitext') THEN NULL "
            "WHEN t.name IN ('varchar', 'char', 'nvarchar', 'nchar', 'sysname', 'longsysname') THEN c.length "
            "ELSE c.prec END, "
            # money has 4 decimals, and the catalog has no scale for it
            "CASE WHEN t.name IN ('money', 'smallmoney') THEN 4 ELSE c.scale END "
            f"FROM {db}..syscolumns c, {db}..systypes t "
            f"WHERE c.usertype = t.usertype AND c.id = object_id('{database}.{owner}.{name}') "
            "ORDER BY c.colid"
        )

    def _normalize_table_path(self, path: DbPath) -> DbPath:
        if len(path) == 1:
            return self.default_database, self.default_schema, path[0]
        elif len(path) == 2:
            return self.default_database, path[0], path[1]
        elif len(path) == 3:
            return path

        raise ValueError(
            f"{self.name}: Bad table path for {self}: '{'.'.join(path)}'. Expected format: table, owner.table, or database.owner.table"
        )

    def _refine_coltypes(
        self, table_path: DbPath, col_dict: Dict[str, ColType], where: Optional[str] = None, sample_size=64
    ):
        original = dict(col_dict)
        # text and unitext can't be keys, and rtrim() rejects them, so they aren't sampled
        lobs = {k: v for k, v in col_dict.items() if _storage(v) in LOB_STORAGE}
        for name in lobs:
            del col_dict[name]
        super()._refine_coltypes(table_path, col_dict, where, sample_size)
        col_dict.update(lobs)
        # Refining by sampling replaces the text type, but hashing still depends on its storage
        for name, refined in col_dict.items():
            before = original[name]
            if refined is before or not isinstance(before, SybaseText):
                continue
            cls = {String_UUID: SybaseStringUUID, String_VaryingAlphanum: SybaseAlphanum}.get(type(refined))
            if cls is None:
                # A type without the storage would be hashed through the wrong path
                logger.warning(f"Not refining {name} to {refined}, which this driver can't hash.")
                col_dict[name] = before
                continue
            fields = {a.name: getattr(refined, a.name) for a in attrs.fields(type(refined)) if a.init}
            col_dict[name] = cls(**fields, storage=before.storage, length=before.length)
        return col_dict

    def _query_cursor(self, c, sql_code: str):
        # ASE says to retry when the procedure cache is full, which parallel diffs of Unicode text
        # can cause. It fails while compiling, before the statement runs, so a retry is safe.
        for delay in (0.5, 1, 2, 4):
            try:
                return self._query_cursor_once(c, sql_code)
            except QueryError as e:
                if "(701)" not in str(e.__cause__):
                    raise
                logger.warning("Sybase ASE is out of procedure cache. Retrying in %ss.", delay)
                time.sleep(delay)
                # The failed cursor is closed
                c = self.thread_local.conn.cursor()
        return self._query_cursor_once(c, sql_code)

    def _query_cursor_once(self, c, sql_code: str):
        try:
            c.execute(sql_code)
            result = None
            if sql_code.lower().startswith("select") and c.description is not None:
                result = QueryResult(c.fetchall(), [col[0] for col in c.description])
            # A batch returns a result for each statement. ASE cancels the statements whose results
            # aren't read, without an error, and an error in one of them only shows up here.
            while c.nextset():
                pass
            return result
        except self._pyodbc.Error as e:
            # A failed statement can leave locks behind until its results are released
            for release in (c.cancel, c.close):
                with suppress(Exception):  # The original error is the one to report
                    release()
            message = str(e)
            if "(701)" in message:
                raise QueryError(
                    f"{e}\nThe query needs more procedure cache than Sybase ASE has. Hashing univarchar "
                    "uses a lot of it, so compare fewer Unicode text columns at a time, or raise "
                    "'procedure cache size'."
                ) from e
            raise QueryError(e) from e
