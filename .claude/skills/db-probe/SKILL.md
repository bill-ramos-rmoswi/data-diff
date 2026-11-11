---
name: db-probe
description: Run SQL on the test databases (PostgreSQL, SQL Server, Db2, SAP ASE, MySQL) to check how each behaves — rounding, casts, hashing, encodings, limits, timings. Use for any spike or "does X work the same on every database" question, instead of writing a pyodbc or psycopg script, or when the user runs /db-probe.
---

# Probing the databases

Use `dev/probe.py`, not a hand-written driver script. It connects through data-diff's own drivers from `DATADIFF_*_URI`, so it sees what the tests see, and it prints at most 20 rows per database, with errors on one line.

```shell
poetry run python dev/probe.py <dbs> "<sql>"            # dbs: postgres,mssql,db2,sybase,mysql or all
poetry run python dev/probe.py <dbs> -f <file.sql>      # for long SQL; write the file to the scratchpad
poetry run python dev/probe.py sybase --perf "<sql>"    # ASE's perfdb (fact_2x, fact_13x) instead of testdb
poetry run python dev/probe.py postgres --repeat 3 "<sql>"   # median time of 3 runs
```

## Before you write the SQL

Read "Database behavior that's easy to get wrong" and "SAP ASE behavior" in CLAUDE.md. Most failed spikes so far hit a limit that's already documented there. The ones people hit most often:

- **Db2** needs `FROM sysibm.sysdummy1` for a `SELECT` with no table, and truncates in `CAST`. Table names are case-sensitive.
- **ASE** expressions stop at 16,384 bytes without an error, and the connection is cp850. Write other characters as `U&'\XXXX'`, and never write `''`. `hash()` on `text`, `unitext` or `image` is wrong without an error.
- **SQL Server** renders `uniqueidentifier` in uppercase, and `FORMAT()` is slow.

## How to probe

1. Run the same question on every database it concerns, in one call where the SQL is portable. Where it isn't, run one call per dialect in the same turn.
2. Compare against the reference: `str_to_checksum` in `tests/common.py` for checksums, and PostgreSQL for rendering.
3. Report a small table with the database, the expression, the result and whether it matches. Results override CLAUDE.md and any prompt. If a result contradicts CLAUDE.md, say so and offer to fix CLAUDE.md.
4. For timing, use `--repeat 3` on the perf tables, and treat a gap under 20% as noise.

## Examples

- `/db-probe Does hash(convert(varchar(16384), t), 'md5') on ASE match PostgreSQL's md5(t) for 'é', '漢' and an emoji? Show both.`
- `/db-probe How does CAST(1.125 AS DECIMAL(3,2)) round on postgres, mssql, db2, sybase?` gives 1.13, 1.13, 1.12, 1.12.
- `/db-probe Time the checksum of fact_13x on Db2 with and without to_string on the integer columns.`
