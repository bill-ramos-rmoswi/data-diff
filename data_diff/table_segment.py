import time
from typing import Container, Dict, List, Optional, Sequence, Tuple
import logging
from itertools import product

import attrs
from typing_extensions import Self

from data_diff.utils import safezip, Vector
from data_diff.utils import ArithString, lob_digest, split_space
from data_diff.databases.base import Database
from data_diff.abcs.database_types import DbPath, DbKey, DbTime, IKey
from data_diff.schema import RawColumnInfo, Schema, create_schema
from data_diff.queries.extras import Checksum
from data_diff.queries.api import Count, SKIP, table, this, Expr, min_, max_, Code, and_, or_, sum_, if_
from data_diff.queries.ast_classes import BinBoolOp, BinOp, In, Select
from data_diff.queries.extras import (
    ApplyFuncAndNormalizeAsString,
    DigestNeedsClient,
    NormalizeAsString,
    RawForClient,
    RowHash,
)

logger = logging.getLogger("table_segment")

RECOMMENDED_CHECKSUM_DURATION = 20


# Columns per integer of get_key_hashes(null_mask=True), so the sum fits in a signed 64-bit integer
NULL_MASK_BITS = 62


def split_key_space(min_key: DbKey, max_key: DbKey, count: int) -> List[DbKey]:
    assert min_key < max_key

    if max_key - min_key <= count:
        count = 1

    if isinstance(min_key, ArithString):
        assert type(min_key) is type(max_key)
        checkpoints = min_key.range(max_key, count)
    else:
        checkpoints = split_space(min_key, max_key, count)

    assert all(min_key < x < max_key for x in checkpoints)
    return [min_key] + checkpoints + [max_key]


def int_product(nums: List[int]) -> int:
    p = 1
    for n in nums:
        p *= n
    return p


def split_compound_key_space(mn: Vector, mx: Vector, count: int) -> List[List[DbKey]]:
    """Returns a list of split-points for each key dimension, essentially returning an N-dimensional grid of split points."""
    return [split_key_space(mn_k, mx_k, count) for mn_k, mx_k in safezip(mn, mx)]


def create_mesh_from_points(*values_per_dim: list) -> List[Tuple[Vector, Vector]]:
    """Given a list of values along each axis of N dimensional space,
    return an array of boxes whose start-points & end-points align with the given values,
    and together consitute a mesh filling that space entirely (within the bounds of the given values).

    Assumes given values are already ordered ascending.

    len(boxes) == ∏i( len(i)-1 )

    Example:
        ::
            >>> d1 = 'a', 'b', 'c'
            >>> d2 = 1, 2, 3
            >>> d3 = 'X', 'Y'
            >>> create_mesh_from_points(d1, d2, d3)
            [
                [('a', 1, 'X'), ('b', 2, 'Y')],
                [('a', 2, 'X'), ('b', 3, 'Y')],
                [('b', 1, 'X'), ('c', 2, 'Y')],
                [('b', 2, 'X'), ('c', 3, 'Y')]
            ]
    """
    assert all(len(v) >= 2 for v in values_per_dim), values_per_dim

    # Create tuples of (v1, v2) for each pair of adjacent values
    ranges = [list(zip(values[:-1], values[1:])) for values in values_per_dim]

    assert all(a <= b for r in ranges for a, b in r)

    # Create a product of all the ranges
    res = [tuple(Vector(a) for a in safezip(*r)) for r in product(*ranges)]

    expected_len = int_product(len(v) - 1 for v in values_per_dim)
    assert len(res) == expected_len, (len(res), expected_len)
    return res


@attrs.define(frozen=True)
class TableSegment:
    """Signifies a segment of rows (and selected columns) within a table

    Parameters:
        database (Database): Database instance. See :meth:`connect`
        table_path (:data:`DbPath`): Path to table in form of a tuple. e.g. `('my_dataset', 'table_name')`
        key_columns (Tuple[str]): Name of the key column, which uniquely identifies each row (usually id)
        update_column (str, optional): Name of updated column, which signals that rows changed.
                                       Usually updated_at or last_update. Used by `min_update` and `max_update`.
        extra_columns (Tuple[str, ...], optional): Extra columns to compare
        min_key (:data:`Vector`, optional): Lowest key value, used to restrict the segment
        max_key (:data:`Vector`, optional): Highest key value, used to restrict the segment
        min_update (:data:`DbTime`, optional): Lowest update_column value, used to restrict the segment
        max_update (:data:`DbTime`, optional): Highest update_column value, used to restrict the segment
        where (str, optional): An additional 'where' expression to restrict the search space.

        case_sensitive (bool): If false, the case of column names will adjust according to the schema. Default is true.

    """

    # Location of table
    database: Database
    table_path: DbPath

    # Columns
    key_columns: Tuple[str, ...]
    update_column: Optional[str] = None
    extra_columns: Tuple[str, ...] = ()
    ignored_columns: Container[str] = frozenset()

    # Restrict the segment
    min_key: Optional[Vector] = None
    max_key: Optional[Vector] = None
    min_update: Optional[DbTime] = None
    max_update: Optional[DbTime] = None
    where: Optional[str] = None

    case_sensitive: Optional[bool] = True
    _schema: Optional[Schema] = None

    def __attrs_post_init__(self) -> None:
        if not self.update_column and (self.min_update or self.max_update):
            raise ValueError("Error: the min_update/max_update feature requires 'update_column' to be set.")

        if self.min_key is not None and self.max_key is not None and self.min_key >= self.max_key:
            raise ValueError(f"Error: min_key expected to be smaller than max_key! ({self.min_key} >= {self.max_key})")

        if self.min_update is not None and self.max_update is not None and self.min_update >= self.max_update:
            raise ValueError(
                f"Error: min_update expected to be smaller than max_update! ({self.min_update} >= {self.max_update})"
            )

    def _where(self) -> Optional[str]:
        return f"({self.where})" if self.where else None

    def _with_raw_schema(self, raw_schema: Dict[str, RawColumnInfo]) -> Self:
        schema = self.database._process_table_schema(self.table_path, raw_schema, self.relevant_columns, self._where())
        return self.new(schema=create_schema(self.database.name, self.table_path, schema, self.case_sensitive))

    def with_schema(self) -> Self:
        "Queries the table schema from the database, and returns a new instance of TableSegment, with a schema."
        if self._schema:
            return self

        return self._with_raw_schema(self.database.query_table_schema(self.table_path))

    def get_schema(self) -> Dict[str, RawColumnInfo]:
        return self.database.query_table_schema(self.table_path)

    def _key_range_expr(self, key_column: str) -> Expr:
        coltype = self._schema[key_column] if self._schema else None
        return self.database.dialect.key_range_expr(this[key_column], coltype)

    def _make_key_range(self):
        if self.min_key is not None:
            for mn, k in safezip(self.min_key, self.key_columns):
                yield BinBoolOp(">=", [self._key_range_expr(k), mn])
        if self.max_key is not None:
            for k, mx in safezip(self.key_columns, self.max_key):
                yield BinBoolOp("<", [self._key_range_expr(k), mx])

    def _make_update_range(self):
        if self.min_update is not None:
            yield self.min_update <= this[self.update_column]
        if self.max_update is not None:
            yield this[self.update_column] < self.max_update

    @property
    def source_table(self):
        return table(*self.table_path, schema=self._schema)

    def make_select(self):
        return self.source_table.where(
            *self._make_key_range(), *self._make_update_range(), Code(self._where()) if self.where else SKIP
        )

    def get_values(self) -> list:
        "Download all the relevant values of the segment from the database"

        # Fetch all the original columns, even if some were later excluded from checking.
        fetched_cols = [NormalizeAsString(this[c]) for c in self.relevant_columns]
        select = self.make_select().select(*fetched_cols)
        return self.database.query(select, List[Tuple])

    def get_key_hashes(self, null_mask: bool = False) -> list:
        """Download the key and a hash of each row of the segment, in one pass.

        Each row is ``(*key, hash, needs_client)``. ``needs_client`` is 1 when the row has a large
        value that the database couldn't hash, so the hash doesn't cover it.

        With ``null_mask``, each row ends with integers whose bits say which of the checked columns
        that aren't keys are NULL, 62 columns to an integer. The hash can't tell NULL from the
        text ``<null>``, and these can.
        """
        checked_columns = [c for c in self.relevant_columns if c not in self.ignored_columns]
        masks = []
        if null_mask:
            columns = [c for c in checked_columns if c not in self.key_columns]
            for start in range(0, len(columns), NULL_MASK_BITS):
                chunk = columns[start : start + NULL_MASK_BITS]
                masks.append(BinOp("+", [if_(this[c] == None, 1 << i, 0) for i, c in enumerate(chunk)]))  # noqa: E711
        select = self.make_select().select(
            *(NormalizeAsString(this[c]) for c in self.key_columns),
            RowHash([NormalizeAsString(this[c]) for c in checked_columns]),
            DigestNeedsClient([this[c] for c in checked_columns]),
            *masks,
        )
        return self.database.query(select, List[Tuple])

    def get_rows_by_keys(self, keys: Sequence[Tuple[str, ...]]) -> List[tuple]:
        """Download the rows with the given keys, as ``get_values`` would return them.

        The keys are normalized strings, as ``get_key_hashes`` returns them. Large values that the
        database couldn't hash are downloaded and hashed here.
        """
        key_types = [self._schema[k] for k in self.key_columns]
        typed_keys = [tuple(t.make_value(v) for t, v in safezip(key_types, key)) for key in keys]
        if len(self.key_columns) == 1:
            has_key = In(this[self.key_columns[0]], [key[0] for key in typed_keys])
        else:
            has_key = or_(*(and_(*(this[c] == v for c, v in safezip(self.key_columns, key))) for key in typed_keys))

        columns = self.relevant_columns
        dialect = self.database.dialect
        on_client = [
            i
            for i, c in enumerate(columns)
            if getattr(self._schema[c], "digest", None) is not None and self._schema[c].digest.client
        ]
        select = self.source_table.where(
            has_key, *self._make_update_range(), Code(self._where()) if self.where else SKIP
        ).select(
            *(NormalizeAsString(this[c]) for c in columns),
            *(DigestNeedsClient([this[columns[i]]]) for i in on_client),
            *(RawForClient(this[columns[i]]) for i in on_client),
        )
        rows = []
        for row in self.database.query(select, List[Tuple]):
            row = list(row)
            flags = row[len(columns) : len(columns) + len(on_client)]
            raws = row[len(columns) + len(on_client) :]
            for needs_client, raw, i in zip(flags, raws, on_client):
                # The flag decides, because some databases return the value for every row
                if needs_client:
                    row[i] = lob_digest(dialect.decode_raw(raw, self._schema[columns[i]]))
            rows.append(tuple(row[: len(columns)]))
        return rows

    def choose_checkpoints(self, count: int) -> List[List[DbKey]]:
        "Suggests a bunch of evenly-spaced checkpoints to split by, including start, end."

        assert self.is_bounded

        # Take Nth root of count, to approximate the appropriate box size
        count = int(count ** (1 / len(self.key_columns))) or 1

        return split_compound_key_space(self.min_key, self.max_key, count)

    def segment_by_checkpoints(self, checkpoints: List[List[DbKey]]) -> List["TableSegment"]:
        "Split the current TableSegment to a bunch of smaller ones, separated by the given checkpoints"

        # Checkpoints come from one of the two tables, so convert them to this table's key types.
        # Otherwise the casing of UUIDs leaks across, and selects no rows where string order is binary.
        key_types = [self._schema[k] for k in self.key_columns] if self._schema else None
        return [
            self.new_key_bounds(min_key=s, max_key=e, key_types=key_types)
            for s, e in create_mesh_from_points(*checkpoints)
        ]

    def new(self, **kwargs) -> Self:
        """Creates a copy of the instance using 'replace()'"""
        return attrs.evolve(self, **kwargs)

    def new_key_bounds(self, min_key: Vector, max_key: Vector, *, key_types: Optional[Sequence[IKey]] = None) -> Self:
        if self.min_key is not None:
            assert self.min_key <= min_key, (self.min_key, min_key)
            assert self.min_key < max_key

        if self.max_key is not None:
            assert min_key < self.max_key
            assert max_key <= self.max_key

        # If asked, enforce the PKs to proper types, mainly to meta-params of the relevant side,
        # so that we do not leak e.g. casing of UUIDs from side A to side B and vice versa.
        # If not asked, keep the meta-params of the keys as is (assume them already casted).
        if key_types is not None:
            min_key = Vector(type.make_value(val) for type, val in safezip(key_types, min_key))
            max_key = Vector(type.make_value(val) for type, val in safezip(key_types, max_key))

        return attrs.evolve(self, min_key=min_key, max_key=max_key)

    @property
    def relevant_columns(self) -> List[str]:
        extras = list(self.extra_columns)

        if self.update_column and self.update_column not in extras:
            extras = [self.update_column] + extras

        return list(self.key_columns) + extras

    def count(self) -> int:
        """Count how many rows are in the segment, in one pass."""
        return self.database.query(self.make_select().select(Count()), int)

    def count_and_checksum(self) -> Tuple[int, int]:
        """Count and checksum the rows in the segment, in one pass."""

        checked_columns = [c for c in self.relevant_columns if c not in self.ignored_columns]
        cols = [NormalizeAsString(this[c]) for c in checked_columns]

        start = time.monotonic()
        has_digest = any(getattr(self._schema[c], "digest", None) is not None for c in checked_columns)
        if has_digest and self.database.dialect.DIGEST_IN_SUBQUERY:
            # The digest of a large value is a subquery, which this database can't aggregate over
            row_hashes = self.make_select().select(row_hash=RowHash(cols))
            q = Select(row_hashes, columns=[Count(), sum_(Code("row_hash"))])
        else:
            q = self.make_select().select(Count(), Checksum(cols))
        count, checksum = self.database.query(q, tuple)
        duration = time.monotonic() - start
        if duration > RECOMMENDED_CHECKSUM_DURATION:
            logger.warning(
                "Checksum is taking longer than expected (%.2f). "
                "We recommend increasing --bisection-factor or decreasing --threads.",
                duration,
            )

        if count:
            assert checksum, (count, checksum)
        return count or 0, int(checksum) if count else None

    def query_key_range(self) -> Tuple[tuple, tuple]:
        """Query database for minimum and maximum key. This is used for setting the initial bounds."""
        # Normalizes the result (needed for UUIDs) after the min/max computation
        select = self.make_select().select(
            ApplyFuncAndNormalizeAsString(this[k], f) for k in self.key_columns for f in (min_, max_)
        )
        result = tuple(self.database.query(select, tuple))

        if any(i is None for i in result):
            raise ValueError("Table appears to be empty")

        # Min/max keys are interleaved
        min_key, max_key = result[::2], result[1::2]
        assert len(min_key) == len(max_key)

        return min_key, max_key

    @property
    def is_bounded(self):
        return self.min_key is not None and self.max_key is not None

    def approximate_size(self):
        if not self.is_bounded:
            raise RuntimeError("Cannot approximate the size of an unbounded segment. Must have min_key and max_key.")
        diff = self.max_key - self.min_key
        assert all(d > 0 for d in diff)
        return int_product(diff)
