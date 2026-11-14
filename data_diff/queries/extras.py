"Useful AST classes that don't quite fall within the scope of regular SQL"

from typing import Callable, Optional, Sequence

import attrs

from data_diff.abcs.database_types import ColType
from data_diff.queries.ast_classes import Expr, ExprNode


@attrs.define(frozen=True)
class NormalizeAsString(ExprNode):
    expr: ExprNode
    expr_type: Optional[ColType] = None

    @property
    def type(self) -> Optional[type]:
        return str


@attrs.define(frozen=True)
class ApplyFuncAndNormalizeAsString(ExprNode):
    expr: ExprNode
    apply_func: Optional[Callable] = None


@attrs.define(frozen=True)
class Checksum(ExprNode):
    exprs: Sequence[Expr]


@attrs.define(frozen=True)
class RowHash(ExprNode):
    """The hash of one row, which is what ``Checksum`` sums."""

    exprs: Sequence[Expr]


@attrs.define(frozen=True)
class LengthPrefixed(ExprNode):
    """The value's length in UTF-8 bytes, ``:``, and the value, or NULL. Leaf row hashes use it, so
    that ``|`` in text can't move between columns."""

    expr: ExprNode

    @property
    def type(self) -> Optional[type]:
        return str


@attrs.define(frozen=True)
class DigestNeedsClient(ExprNode):
    """1 for a row with a large value that the database can't hash, which the client must fetch."""

    exprs: Sequence[Expr]


@attrs.define(frozen=True)
class RawForClient(ExprNode):
    """The value itself where the database can't hash it, and NULL elsewhere."""

    expr: ExprNode
