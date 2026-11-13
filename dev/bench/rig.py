"""Set each database to use the resources docker-compose.bench.yml gives it, and check that it does.

    poetry run python dev/bench/rig.py check              # read the settings, write .runs/rig.json
    poetry run python dev/bench/rig.py check --noise 5    # also time the fact_13x checksum 5 times each, ~4 min
    poetry run python dev/bench/rig.py apply              # set the bench profile, then check
    poetry run python dev/bench/rig.py apply --profile everyday

Start the databases with the bench overlay first (see docker-compose.bench.yml). The bench
profile gives every engine the same rule inside its 4 vCPU / 4 GB container: a buffer cache of
about 2 GB, and queries allowed 4 parallel workers.

| Engine | Memory | Parallelism | Applied by |
|---|---|---|---|
| PostgreSQL | shared_buffers 2GB | max_parallel_workers_per_gather 4 | the overlay's command |
| SQL Server | max server memory 2560 MB | MAXDOP 4 | this script, dynamic |
| Db2 | INSTANCE_MEMORY 3.5 GB, IBMDEFAULTBP 2 GB | INTRA_PARALLEL, MAX_QUERYDEGREE 4, DFT_DEGREE ANY | this script; INTRA_PARALLEL needs a restart |
| SAP ASE | max memory 3.25 GB, default data cache 2 GB | 4 online engines, max parallel degree 4 | this script, after dev/ase/init.sql |

ASE runs in threaded kernel mode, where the thread pool sets the engines (the image has 4 online)
and `number of engines at startup` is rejected, so the check reads the engines that are online.

Settings that need a restart are reported as pending, with the WSL command that restarts the
container. Every statement's error is printed on one line and the rest carry on, so check the
output: a setting this script couldn't change shows its old value.

The settings stay after the benchmark: SQL Server's and Db2's in their volumes, ASE's in the
container's ASE.cfg until it's recreated. `--profile everyday` puts SQL Server and Db2 back, and
says how to recreate ASE, whose image settings come back with the container.

`check` writes what it read to .runs/rig.json, and dev/perf/run_perf.py copies it into every
result, so results from different rigs can be told apart.
"""

import argparse
import json
import os
import platform
import statistics
import sys
from datetime import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "dev", "perf"))

URI_VARS = {"PostgreSQL": "POSTGRESQL", "MsSQL": "MSSQL", "Db2": "DB2", "Sybase": "SYBASE"}

READS = {
    "PostgreSQL": [
        "SELECT name, current_setting(name) FROM pg_settings WHERE name IN ('shared_buffers', "
        "'effective_cache_size', 'work_mem', 'max_worker_processes', 'max_parallel_workers', "
        "'max_parallel_workers_per_gather') ORDER BY name",
    ],
    "MsSQL": [
        "SELECT 'cpu_count', CAST(cpu_count AS varchar(20)) FROM sys.dm_os_sys_info "
        "UNION ALL SELECT 'scheduler_count', CAST(scheduler_count AS varchar(20)) FROM sys.dm_os_sys_info "
        "UNION ALL SELECT 'physical_memory_mb', CAST(physical_memory_kb / 1024 AS varchar(20)) FROM sys.dm_os_sys_info",
        "SELECT name, CAST(value_in_use AS varchar(20)) FROM sys.configurations "
        "WHERE name IN ('max degree of parallelism', 'max server memory (MB)', 'cost threshold for parallelism')",
    ],
    "Db2": [
        "SELECT 'cpu_online', VARCHAR(CPU_ONLINE) FROM TABLE(SYSPROC.ENV_GET_SYSTEM_RESOURCES())",
        "SELECT 'memory_total_mb', VARCHAR(MEMORY_TOTAL) FROM TABLE(SYSPROC.ENV_GET_SYSTEM_RESOURCES())",
        "SELECT NAME, VALUE || CASE WHEN DEFERRED_VALUE <> VALUE THEN ' (pending ' || DEFERRED_VALUE || ', "
        "needs a restart)' ELSE '' END FROM SYSIBMADM.DBMCFG "
        "WHERE NAME IN ('intra_parallel', 'max_querydegree', 'instance_memory')",
        "SELECT NAME, VALUE FROM SYSIBMADM.DBCFG WHERE NAME IN ('dft_degree', 'self_tuning_mem')",
        "SELECT 'bufferpool ' || BPNAME, VARCHAR(NPAGES) || ' pages of ' || VARCHAR(PAGESIZE) FROM SYSCAT.BUFFERPOOLS",
    ],
    "Sybase": [
        "select 'online engines', convert(varchar(20), count(*)) from master..sysengines where status = 'online'",
        "select c.comment, convert(varchar(20), cc.value) from master..sysconfigures c, master..syscurconfigs cc "
        "where c.config = cc.config and c.comment in ('max memory', 'number of engines at startup', "
        "'max online engines', 'max parallel degree', 'number of worker processes', 'procedure cache size')",
        "select 'cache ' + cache_name, convert(varchar(30), run_size) from master..syscacheinfo",
    ],
}

DB2_RESTART = (
    'docker exec dd-db2 su - db2inst1 -c "db2 force applications all; db2stop force; db2start; db2 activate db testdb"'
)
ASE_RECREATE = "docker compose --profile ase up -d --wait --force-recreate ase"

PROFILES = {
    "bench": {
        "MsSQL": [
            "EXEC sp_configure 'show advanced options', 1",
            "RECONFIGURE",
            "EXEC sp_configure 'max degree of parallelism', 4",
            "EXEC sp_configure 'max server memory (MB)', 2560",
            "RECONFIGURE",
        ],
        "Db2": [
            "CALL SYSPROC.ADMIN_CMD('UPDATE DBM CFG USING INTRA_PARALLEL YES MAX_QUERYDEGREE 4 INSTANCE_MEMORY 917504')",
            "CALL SYSPROC.ADMIN_CMD('UPDATE DB CFG USING DFT_DEGREE ANY')",
            # 2 GB of the bufferpool's page size, which is 4 KB unless the database was made otherwise
            "BUFFERPOOL 2147483648",
        ],
        "Sybase": [
            "exec sp_configure 'max memory', 1703936",
            "exec sp_cacheconfig 'default data cache', '2048M'",
            "exec sp_configure 'max online engines', 4",
            "exec sp_configure 'number of worker processes', 16",
            "exec sp_configure 'max parallel degree', 4",
        ],
    },
    "everyday": {
        "MsSQL": [
            "EXEC sp_configure 'show advanced options', 1",
            "RECONFIGURE",
            "EXEC sp_configure 'max degree of parallelism', 0",
            "EXEC sp_configure 'max server memory (MB)', 2147483647",
            "RECONFIGURE",
        ],
        "Db2": [
            "CALL SYSPROC.ADMIN_CMD('UPDATE DBM CFG USING INTRA_PARALLEL NO MAX_QUERYDEGREE ANY INSTANCE_MEMORY AUTOMATIC')",
            "CALL SYSPROC.ADMIN_CMD('UPDATE DB CFG USING DFT_DEGREE 1')",
            "ALTER BUFFERPOOL IBMDEFAULTBP SIZE AUTOMATIC",
        ],
        "Sybase": [],
    },
}

# What check compares against, to label the rig: the values each engine may print. Db2 isn't
# checked for 4 CPUs, because ENV_GET_SYSTEM_RESOURCES reports the host's CPUs inside a cpuset
# too (`docker exec dd-db2 nproc` shows the pin), and Community Edition uses at most 4 cores.
EXPECTED_BENCH = {
    "PostgreSQL": {"shared_buffers": ("2GB",), "max_parallel_workers_per_gather": ("4",)},
    "MsSQL": {"cpu_count": ("4",), "max degree of parallelism": ("4",), "max server memory (MB)": ("2560",)},
    # DFT_DEGREE ANY reads back as -1
    "Db2": {"intra_parallel": ("YES",), "max_querydegree": ("4",), "dft_degree": ("ANY", "-1")},
    # dev/ase/init.sql sets the procedure cache, which a recreated container loses
    "Sybase": {"online engines": ("4",), "max parallel degree": ("4",), "procedure cache size": ("120000",)},
}


def one_line(e: BaseException) -> str:
    return f"{type(e).__name__}: {' '.join(str(e).split())[:300]}"


def configured() -> list:
    return [name for name, var in URI_VARS.items() if os.environ.get(f"DATADIFF_{var}_URI")]


def connect(name: str):
    from data_diff import connect as dd_connect

    return dd_connect(os.environ[f"DATADIFF_{URI_VARS[name]}_URI"])


def run_statement(db, name: str, sql: str):
    if name == "MsSQL":
        # RECONFIGURE fails inside a transaction, and the driver's connections are never in
        # autocommit, so these run on a connection of their own
        conn = db.create_connection()
        try:
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(sql)
            while cursor.nextset():
                pass
        finally:
            conn.close()
    else:
        db.query(sql, None)


def apply(name: str, profile: str) -> list:
    """Runs the profile's statements on one database. Returns notes for the report."""
    statements = PROFILES[profile].get(name, [])
    notes = []
    if name == "Sybase":
        # A recreated container has lost the procedure cache setting and the rest of init.sql
        sys.path.insert(0, os.path.join(REPO, "dev", "ase"))
        import init as ase_init

        try:
            ase_init.apply()
            print("  [Sybase] ok: dev/ase/init.sql")
        except Exception as e:
            print(f"  [Sybase] failed: dev/ase/init.sql: {one_line(e)}")
    if not statements:
        if name == "Sybase" and profile == "everyday":
            notes.append(f"ASE: recreate the container to get the image's settings back, in WSL: {ASE_RECREATE}")
        return notes
    db = connect(name)
    try:
        for sql in statements:
            if sql.startswith("BUFFERPOOL "):
                size = int(sql.split()[1])
                page = db.query("SELECT PAGESIZE FROM SYSCAT.BUFFERPOOLS WHERE BPNAME = 'IBMDEFAULTBP'", int)
                sql = f"ALTER BUFFERPOOL IBMDEFAULTBP SIZE {size // page}"
            try:
                run_statement(db, name, sql)
                print(f"  [{name}] ok: {sql}")
            except Exception as e:
                print(f"  [{name}] failed: {sql}: {one_line(e)}")
    finally:
        db.close()
    if name == "Db2":
        notes.append(f"Db2: INTRA_PARALLEL and INSTANCE_MEMORY apply after a restart, in WSL: {DB2_RESTART}")
    return notes


def read(name: str) -> dict:
    values = {}
    db = connect(name)
    try:
        for sql in READS[name]:
            try:
                for setting, value in db.query(sql, list) or []:
                    values[str(setting).strip()] = str(value).strip()
            except Exception as e:
                values[f"error in: {sql[:60]}"] = one_line(e)
    finally:
        db.close()
    return values


def measure_noise(name: str, repeat: int, table: str) -> dict:
    from checksum import spread, time_checksum

    try:
        count, checksum, seconds, _ = time_checksum(name, table, repeat)
    except Exception as e:
        return {"error": one_line(e)}
    return {
        "table": table,
        "median_sec": round(statistics.median(seconds), 3),
        "spread_pct": round(100 * spread(seconds), 1),
        "runs": [round(s, 3) for s in seconds],
        "checksum": str(checksum),
    }


def check(noise: int, table: str = "fact_13x") -> dict:
    rig = {"checked_at": datetime.now().isoformat(timespec="seconds"), "host": platform.node(), "engines": {}}
    matches = []
    for name in configured():
        try:
            values = read(name)
        except Exception as e:
            values = {"connect failed": one_line(e)}
        expected = EXPECTED_BENCH.get(name, {})
        mismatches = {k: (values.get(k), " or ".join(v)) for k, v in expected.items() if values.get(k) not in v}
        matches.append(not mismatches)
        engine = {
            "settings": values,
            "differs_from_bench": {k: {"is": a, "bench": b} for k, (a, b) in mismatches.items()},
        }
        if noise:
            print(f"  timing the {table} checksum {noise} times on {name}...", flush=True)
            engine["noise"] = measure_noise(name, noise, table)
        rig["engines"][name] = engine
        print(f"[{name}]")
        for setting, value in values.items():
            print(f"  {setting}: {value}")
        for setting, (actual, wanted) in mismatches.items():
            print(f"  ! {setting} is {actual}, bench wants {wanted}")
        if noise:
            n = engine["noise"]
            if "error" in n:
                print(f"  noise: {n['error']}")
            else:
                print(f"  {table} checksum x{noise}: median {n['median_sec']}s, spread {n['spread_pct']}%")
    rig["profile"] = "bench" if matches and all(matches) else "not bench"
    print(f"Rig: {rig['profile']}")
    print(
        "Visible CPUs and limits, from WSL: for c in dd-postgresql dd-mssql dd-db2 dd-ase; do echo $c "
        "$(docker exec $c nproc) $(docker inspect -f '{{.HostConfig.CpusetCpus}} {{.HostConfig.Memory}}' $c); done"
    )
    os.makedirs(os.path.join(REPO, ".runs"), exist_ok=True)
    with open(os.path.join(REPO, ".runs", "rig.json"), "w", encoding="utf-8") as file:
        json.dump(rig, file, indent=2)
    return rig


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["check", "apply"])
    p.add_argument("--profile", choices=sorted(PROFILES), default="bench")
    p.add_argument("--noise", type=int, default=0, help="Time the checksum N times on each database")
    p.add_argument(
        "--noise-table",
        default="fact_13x",
        help="Table for --noise (default fact_13x). fact_2x's checksums take under a second on some databases, "
        "so its spread is mostly timer jitter",
    )
    args = p.parse_args()

    notes = []
    if args.action == "apply":
        for name in configured():
            print(f"Applying {args.profile} to {name}")
            try:
                notes += apply(name, args.profile)
            except Exception as e:
                print(f"  [{name}] connect failed: {one_line(e)}")
    check(args.noise, args.noise_table)
    for note in notes:
        print(note)


if __name__ == "__main__":
    main()
