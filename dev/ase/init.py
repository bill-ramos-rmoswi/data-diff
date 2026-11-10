"""Applies dev/ase/init.sql to the SAP ASE test server, over TDS. Safe to run more than once.

    poetry run python dev/ase/init.py [sybase://sa:Password1@ase:5000/testdb/dbo]
"""

import os
import re
import sys
from pathlib import Path

INIT_SQL = Path(__file__).with_name("init.sql")
DEFAULT_URI = "sybase://sa:Password1@ase:5000/testdb/dbo"


def batches(sql: str):
    """Splits a script on lines that hold only 'go', as isql does."""
    for batch in re.split(r"^\s*go\s*$", sql, flags=re.IGNORECASE | re.MULTILINE):
        lines = [line for line in batch.splitlines() if line.strip() and not line.strip().startswith("--")]
        if lines:
            yield "\n".join(lines)


def apply(uri: str = None, script: Path = INIT_SQL) -> None:
    from data_diff import connect

    db = connect(uri or os.environ.get("DATADIFF_SYBASE_URI") or DEFAULT_URI, shared=False)

    def run(conn):
        # One connection, because "use" applies to the batches after it
        cursor = conn.cursor()
        for batch in batches(script.read_text()):
            cursor.execute(batch)
            while cursor.nextset():
                pass
        cursor.close()

    try:
        # Raises the ConnectError if the server can't be reached
        db._run_on_connection(run)
    finally:
        db.close()


if __name__ == "__main__":
    apply(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Applied {INIT_SQL.name}")
