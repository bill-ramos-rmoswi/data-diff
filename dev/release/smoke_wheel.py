#!/usr/bin/env python3
"""Install a built wheel in a clean virtual environment and check that it works.

    python3 dev/release/smoke_wheel.py dist/data_diff-0.15.0-py3-none-any.whl
    python3 dev/release/smoke_wheel.py <wheel> --extras duckdb       # fewer extras, for a quick run

It installs the wheel with the extras for the supported databases and imports each driver the way
data-diff does. It runs a DuckDB-to-DuckDB diff through the CLI and through the Python API, and checks
the exact rows reported. It needs no database server. pr-validate.yml runs it on every pull request,
and release.yml on the files it publishes.

The PostgreSQL extra builds psycopg2, and the ODBC drivers need unixODBC, so on Ubuntu install
libpq-dev and unixodbc-dev first. The virtual environment is outside the repository, so the
installed wheel is imported and not the source tree.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

EXTRAS = "postgresql,mssql,db2,sybase,mysql,duckdb"

# Runs inside the new environment. argv: work dir, extras.
CHECK = textwrap.dedent(
    r"""
    import os, subprocess, sys
    work, extras = sys.argv[1], sys.argv[2].split(",")

    import data_diff
    from data_diff.version import __version__
    assert os.path.abspath(data_diff.__file__).startswith(sys.prefix), f"imported {data_diff.__file__}, not the wheel"
    print(f"data_diff {__version__} from {os.path.dirname(data_diff.__file__)}")

    helpers = {
        "postgresql": ("data_diff.databases.postgresql", "import_postgresql"),
        "mssql": ("data_diff.databases.mssql", "import_mssql"),
        "db2": ("data_diff.databases.db2", "import_db2"),
        "sybase": ("data_diff.databases.sybase", "import_sybase"),
        "mysql": ("data_diff.databases.mysql", "import_mysql"),
        "duckdb": ("data_diff.databases.duckdb", "import_duckdb"),
    }
    for extra in extras:
        module, func = helpers[extra]
        getattr(__import__(module, fromlist=[func]), func)()
        print(f"driver ok: {extra}")

    import duckdb
    a, b = os.path.join(work, "a.duckdb"), os.path.join(work, "b.duckdb")
    for path, changed in ((a, False), (b, True)):
        con = duckdb.connect(path)
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR, amount DECIMAL(10,2), ts TIMESTAMP)")
        con.execute(
            "INSERT INTO t SELECT i, 'name ' || i, i * 1.25, TIMESTAMP '2026-01-01' + i * INTERVAL 1 MINUTE "
            "FROM range(1, 5001) r(i)"
        )
        if changed:
            con.execute("UPDATE t SET name = 'changed' WHERE id = 1234")
            con.execute("DELETE FROM t WHERE id = 4321")
        con.close()
    expected = [("-", "1234"), ("+", "1234"), ("-", "4321")]

    from data_diff import connect_to_table, diff_tables
    t1 = connect_to_table(f"duckdb://{a}", "t", "id", extra_columns=("name", "amount", "ts"))
    t2 = connect_to_table(f"duckdb://{b}", "t", "id", extra_columns=("name", "amount", "ts"))
    got = sorted((sign, row[0]) for sign, row in diff_tables(t1, t2, bisection_threshold=500, bisection_factor=4))
    assert got == sorted(expected), f"API diff: got {got}, expected {sorted(expected)}"
    print(f"API diff ok: {got}")
    t1.database.close()
    t2.database.close()

    # Two databases, so the CLI picks hashdiff. Lines look like "- 1234, name 1234, 1542.50, 2026-01-01 20:34:00"
    cmd = [sys.executable, "-m", "data_diff", f"duckdb://{a}", "t", f"duckdb://{b}", "t",
           "-k", "id", "-c", "name", "-c", "amount", "-c", "ts",
           "--bisection-threshold", "500", "--bisection-factor", "4"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"CLI exited {res.returncode}:\n{res.stderr[-2000:]}"
    got = sorted((l[0], l[1:].split(",")[0].strip()) for l in res.stdout.splitlines() if l[:2] in ("- ", "+ "))
    assert got == sorted(expected), f"CLI diff: got {got} from:\n{res.stdout}"
    print(f"CLI diff ok: {got}")
    """
)


def run(cmd, **kw):
    shown = " ".join("<check script>" if c is CHECK else str(c) for c in cmd)
    print("+", shown, flush=True)
    code = subprocess.run(cmd, **kw).returncode
    if code:
        sys.exit(f"Smoke test FAILED (exit {code}): {shown}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wheel", type=Path)
    ap.add_argument("--extras", default=EXTRAS, help=f"comma-separated extras to install (default {EXTRAS})")
    args = ap.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        sys.exit(f"Not a wheel: {wheel}")
    extras = ",".join(e for e in args.extras.split(",") if e)
    if "duckdb" not in extras.split(","):
        extras += ",duckdb"  # the diff needs it

    with tempfile.TemporaryDirectory(prefix="dd-smoke-") as tmp:
        venv = Path(tmp) / "venv"
        run([sys.executable, "-m", "venv", venv])
        py = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
        run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "--quiet", f"{wheel}[{extras}]"])
        run([py, "-I", "-c", CHECK, tmp, extras], cwd=tmp)
    print(f"Smoke test passed: {wheel.name} with [{extras}]")


if __name__ == "__main__":
    main()
