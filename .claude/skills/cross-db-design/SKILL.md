---
name: cross-db-design
description: Planning checklist for any change that must behave the same on every database — checksums, normalize_* methods, md5_as_int, digests, type mapping, key ranges — and for any task whose scope (pairs, sizes, audience) isn't stated. Use in plan mode before designing, or when the user runs /cross-db-design.
---

# Designing a change across databases

Hashdiff works only if every database produces byte-identical strings for equal values. A design that's proven on one dialect isn't a design yet. Twice so far a plan has been rejected or redone for one of these reasons:

- It was designed for ASE alone. The user asked "would this be the same for SQL Server and Db2?"
- The benchmark PR covered 4 GB and every pair, but the user wanted 2 GB and on-prem → Aurora PostgreSQL only.

## 1. Scope first

If the request doesn't say these, ask them in one AskUserQuestion before planning:

- **Pairs:** which directions matter. The default use case is Db2, SQL Server and ASE → PostgreSQL (Aurora).
- **Size and settings:** row counts, memory (ASE 2 GB defaults), threads.
- **Audience:** README readers, CI, or the user only.
- **Compatibility:** may checksum values change? Normally no, unless every dialect changes in the same release.

## 2. A behavior table before the plan

For every expression the change adds or alters, fill this table with probed values, not general knowledge. Use `/db-probe`, one call per row.

| | PostgreSQL | SQL Server | Db2 | ASE | MySQL |
| --- | --- | --- | --- | --- | --- |
| The SQL for it | | | | | |
| Result for the test values | | | | | |
| Rounds or truncates | round | round | truncate | truncate | |
| Encoding hashed | UTF-8 | UTF-16 (NVARCHAR) | UTF-8 | cp850, or UTF-16 for uni* | |
| Length limit, and whether it errors or truncates | | | | 16 KB, silently | |
| Index use or speed effect | | | | | |

The test values must include NULL, the empty string, trailing spaces, non-ASCII characters (é, 漢, an emoji), the maximum precision, negative numbers under 1, and the largest and smallest dates.

**Probe the SQL data-diff generates, on the real tables.** A hand-written query models the types you expect, not the ones data-diff detects. Run the diff with `LOG_LEVEL=DEBUG`, take the SQL and the detected column types from the log, and put the types in the table. In the tuning run, SQL Server `numeric` turned out to be `UnknownColType`.

## 2b. Key shapes and change density

A change to the differ, key ranges, leaves or fetch by key must be tested on each of these, not only on integer keys with a few changes:

| Key shape | Why |
| --- | --- |
| integer | the common case |
| composite (`OrderId, LineNumber`) | key tuples and their ordering |
| native UUID (`uuid`, `uniqueidentifier`) | SQL Server sorts and renders it differently |
| text UUID in mixed case | a normalized key may not select its row. That silently lost differences in the tuning run (F1) |
| non-UUID text among text UUID keys | key types come from 64 sampled rows. This one aborted the diff (F2) |
| rows with many NULL columns | NULL masks, and NULL against the text `<null>` |

| Density of changes in a segment | Why |
| --- | --- |
| none | nothing should be downloaded |
| sparse (1 in 1,000) | the usual case |
| more than half | a fetch by key can cost more than downloading every row (F3) |
| all | the worst case |

## 3. Plan rules

- Prefer a dialect override to a change in `base.py`. A shared change must keep the SQL other dialects produce identical, unless a test shows it's a bug.
- A silent truncation is a false negative. Use the form that raises an error, and name the limit in the message.
- Pair every "should match" test with a "difference is reported" test.
- Name every public default or behavior that changes (an API default, result order, a new path on by default), so it can be decided before implementing rather than at a STOP.
- List the stop conditions: what result would make you stop and report instead of working around it.
- Name the skill for each later step: `/test`, `/perf`, `/code-review`, `/ship`.

## Examples

- `/cross-db-design Compare text/unitext/image LOBs by digest instead of value.`
- `/cross-db-design Benchmarks for the README` asks about pairs, sizes and audience first.
- `/cross-db-design Skip to_string for integer columns in the checksum.`
