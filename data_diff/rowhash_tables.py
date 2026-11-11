"""Compares two tables by reading each one once.

``HashDiffer`` checksums a whole table, and when the checksums differ, checksums it again in
smaller and smaller segments. That's the fastest way to confirm that two tables match, but each
level reads the differing parts of the table again, and hashes their large values again.

``RowHashDiffer`` asks each database for the key and a hash of every row, once, and compares the
hashes here. Only the rows whose hashes differ are read again, by key.
"""

import logging
import math
import time
from typing import Dict, List, Sequence, Tuple

import attrs

from data_diff.abcs.database_types import JSON
from data_diff.hashdiff_tables import HashDiffer, diff_sets
from data_diff.info_tree import InfoTree
from data_diff.table_segment import TableSegment
from data_diff.thread_utils import ThreadedYielder
from data_diff.utils import safezip

logger = logging.getLogger("rowhash_tables")

DEFAULT_SEGMENT_ROWS = 200_000
DEFAULT_KEY_BATCH = 500
MAX_SEGMENTS = 4096

_MISSING = object()


@attrs.define(frozen=False)
class RowHashDiffer(HashDiffer):
    """Finds the diff between two SQL tables, reading each table once.

    Works across databases, like :class:`HashDiffer`. Each database sends the key and a hash of
    every row, about 40 bytes for each, so unlike ``HashDiffer`` the network cost grows with the
    table, not with the differences.

    Parameters:
        segment_rows (int): About how many keys each query covers. Segments run in parallel.
        key_batch (int): How many differing rows to download in one query.
        recheck_delay (float): Seconds to wait before reading the differing rows a second time, and
                               reporting only those that still differ. For tables that are still
                               being replicated. 0 disables it.
        threaded (bool): Enable/disable threaded diffing. Needed to take advantage of database threads.
        max_threadpool_size (int): Maximum size of each threadpool. ``None`` means auto.
    """

    # Values that a database can't hash are downloaded and hashed here, instead of failing
    DIGEST_ON_CLIENT = True

    segment_rows: int = DEFAULT_SEGMENT_ROWS
    key_batch: int = DEFAULT_KEY_BATCH
    recheck_delay: float = 0

    def __attrs_post_init__(self) -> None:
        super().__attrs_post_init__()
        if self.segment_rows < 1 or self.key_batch < 1:
            raise ValueError("segment_rows and key_batch must be at least 1")

    def _bisect_and_diff_segments(
        self,
        ti: ThreadedYielder,
        table1: TableSegment,
        table2: TableSegment,
        info_tree: InfoTree,
        level=0,
        max_rows=None,
    ):
        assert table1.is_bounded and table2.is_bounded

        with self._ignored_columns_lock:
            table1 = attrs.evolve(table1, ignored_columns=frozenset(self.ignored_columns1))
            table2 = attrs.evolve(table2, ignored_columns=frozenset(self.ignored_columns2))

        # The key range is the only size known without reading the table
        biggest_table = max(table1, table2, key=lambda t: t.approximate_size())
        count = min(math.ceil(biggest_table.approximate_size() / self.segment_rows), MAX_SEGMENTS)
        if count > 1:
            checkpoints = biggest_table.choose_checkpoints(count - 1)
            segmented1 = table1.segment_by_checkpoints(checkpoints)
            segmented2 = table2.segment_by_checkpoints(checkpoints)
        else:
            segmented1, segmented2 = [table1], [table2]

        for i, (t1, t2) in enumerate(safezip(segmented1, segmented2)):
            info_node = info_tree.add_node(t1, t2, max_rows=max_rows)
            ti.submit(
                self._diff_segments, ti, t1, t2, info_node, max_rows, level + 1, i + 1, len(segmented1), priority=level
            )

    def _diff_segments(
        self,
        ti: ThreadedYielder,
        table1: TableSegment,
        table2: TableSegment,
        info_tree: InfoTree,
        max_rows: int,
        level=0,
        segment_index=None,
        segment_count=None,
    ):
        logger.info(
            ". " * level + f"Hashing the rows of segment {segment_index}/{segment_count}, "
            f"key-range: {table1.min_key}..{table2.max_key}"
        )
        hashes1, hashes2 = self._threaded_call("get_key_hashes", [table1, table2])
        info_tree.info.rowcounts = {1: len(hashes1), 2: len(hashes2)}
        self.stats["rows_hashed"] = self.stats.get("rows_hashed", 0) + max(len(hashes1), len(hashes2))

        key_size = len(table1.key_columns)
        suspects = set()

        def by_key(rows) -> Dict[tuple, int]:
            hashes = {}
            for row in rows:
                key = tuple(row[:key_size])
                # A duplicate key, or a value the database left for the client to hash
                if key in hashes or row[key_size + 1]:
                    suspects.add(key)
                hashes[key] = int(row[key_size])
            return hashes

        by_key1, by_key2 = by_key(hashes1), by_key(hashes2)
        del hashes1, hashes2
        suspects.update(key for key, value in by_key1.items() if by_key2.get(key, _MISSING) != value)
        suspects.update(key for key in by_key2 if key not in by_key1)
        del by_key1, by_key2

        if not suspects:
            info_tree.info.is_diff = False
            return None

        keys = sorted(suspects)
        diff = self._diff_rows(table1, table2, keys)
        if diff and self.recheck_delay:
            # A row that changed while it was being copied differs now, and not after the copy
            # catches up
            time.sleep(self.recheck_delay)
            keys = sorted({tuple(row[:key_size]) for _, row in diff})
            diff = self._diff_rows(table1, table2, keys)

        info_tree.info.set_diff(diff)
        logger.info(". " * level + f"Diff found {len(diff)} different rows.")
        return diff

    def _diff_rows(self, table1: TableSegment, table2: TableSegment, keys: Sequence[tuple]) -> List[Tuple[str, tuple]]:
        "Download the rows with these keys from both tables, and compare them"
        rows1, rows2 = [], []
        for start in range(0, len(keys), self.key_batch):
            batch = keys[start : start + self.key_batch]
            fetched1, fetched2 = self._thread_map(lambda table: table.get_rows_by_keys(batch), [table1, table2])
            rows1 += fetched1
            rows2 += fetched2
        self.stats["rows_downloaded"] = self.stats.get("rows_downloaded", 0) + max(len(rows1), len(rows2))

        json_cols = {
            i: colname for i, colname in enumerate(table1.extra_columns) if isinstance(table1._schema[colname], JSON)
        }
        return list(
            diff_sets(
                rows1,
                rows2,
                json_cols=json_cols,
                columns1=table1.relevant_columns,
                columns2=table2.relevant_columns,
                key_columns1=table1.key_columns,
                key_columns2=table2.key_columns,
                ignored_columns1=self.ignored_columns1,
                ignored_columns2=self.ignored_columns2,
            )
        )
