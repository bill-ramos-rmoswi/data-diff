# Technical explanation

data-diff can diff tables within the same database, or across different databases.

**Same-DB Diff:**
- Uses an outer-join to diff the rows as efficiently and accurately as possible.
- Supports materializing the diff results to a database table.
- Can also collect various extra statistics about the tables.

**Cross-DB Diff:** Employs a divide and conquer algorithm based on hashing, optimized for few changes.

The following is a technical explanation of the cross-db diff.

### Overview

data-diff splits the table into smaller segments, then checksums each segment in both databases. When the checksums for a segment aren't equal, it will further divide that segment into yet smaller segments, checksumming those until it gets to the differing row(s).

This approach has performance within an order of magnitude of count(*) when there are few/no changes, but is able to output each differing row! By pushing the compute into the databases, it's much faster than querying for and comparing every row.

![Performance for 100M rows](https://user-images.githubusercontent.com/97400/175182987-a3900d4e-c097-4732-a4e9-19a40fac8cdc.png)

**†:** The implementation for downloading all rows that `data-diff` and
`count(*)` is compared to is not optimal. It is a single Python multi-threaded
process. The performance is fairly driver-specific, e.g. PostgreSQL's performs 10x
better than MySQL.

### Deep Dive

In this section we'll be doing a walk-through of exactly how **data-diff**
works, and how to tune `--bisection-factor` and `--bisection-threshold`.

Let's consider a scenario with an `orders` table with 1M rows. Fivetran is
replicating it contionously from PostgreSQL to Snowflake:

```
┌─────────────┐                        ┌─────────────┐
│ PostgreSQL  │                        │  Snowflake  │
├─────────────┤                        ├─────────────┤
│             │                        │             │
│             │                        │             │
│             │  ┌─────────────┐       │ table with  │
│ table with  ├──┤ replication ├──────▶│ ?maybe? all │
│lots of rows!│  └─────────────┘       │  the same   │
│             │                        │    rows.    │
│             │                        │             │
│             │                        │             │
│             │                        │             │
└─────────────┘                        └─────────────┘
```

In order to check whether the two tables are the same, **data-diff** splits
the table into `--bisection-factor=10` segments.

We also have to choose which columns we want to checksum. In our case, we care
about the primary key, `--key-column=id` and the update column
`--update-column=updated_at`. `updated_at` is updated every time the row is, and
we have an index on it.

**data-diff** starts by querying both databases for the `min(id)` and `max(id)`
of the table. Then it splits the table into `--bisection-factor=10` segments of
`1M/10 = 100K` keys each:

```
┌──────────────────────┐              ┌──────────────────────┐
│     PostgreSQL       │              │      Snowflake       │
├──────────────────────┤              ├──────────────────────┤
│      id=1..100k      │              │      id=1..100k      │
├──────────────────────┤              ├──────────────────────┤
│    id=100k..200k     │              │    id=100k..200k     │
├──────────────────────┤              ├──────────────────────┤
│    id=200k..300k     ├─────────────▶│    id=200k..300k     │
├──────────────────────┤              ├──────────────────────┤
│    id=300k..400k     │              │    id=300k..400k     │
├──────────────────────┤              ├──────────────────────┤
│         ...          │              │         ...          │
├──────────────────────┤              ├──────────────────────┤
│      900k..100k      │              │      900k..100k      │
└───────────────────▲──┘              └▲─────────────────────┘
                    ┃                  ┃
                    ┃                  ┃
                    ┃ checksum queries ┃
                    ┃                  ┃
                  ┌─┻──────────────────┻────┐
                  │        data-diff        │
                  └─────────────────────────┘
```

Now **data-diff** will start running `--threads=1` queries in parallel that
checksum each segment. The queries for checksumming each segment will look
something like this, depending on the database:

```sql
SELECT count(*),
    sum(cast(conv(substring(md5(concat(cast(id as char), cast(timestamp as char))), 18), 16, 10) as unsigned))
FROM `rating_del1`
WHERE (id >= 1) AND (id < 100000)
```

This keeps the amount of data that has to be transferred between the databases
to a minimum, making it very performant! Additionally, if you have an index on
`updated_at` (highly recommended), then the query will be fast, as the database
only has to do a partial index scan between `id=1..100k`.

If you are not sure whether the queries are using an index, you can run it with
`--interactive`. This puts **data-diff** in interactive mode, where it shows an
`EXPLAIN` before executing each query, requiring confirmation to proceed.

After running the checksum queries on both sides, we see that all segments
are the same except `id=100k..200k`:

```
┌──────────────────────┐              ┌──────────────────────┐
│     PostgreSQL       │              │      Snowflake       │
├──────────────────────┤              ├──────────────────────┤
│    checksum=0102     │              │    checksum=0102     │
├──────────────────────┤   mismatch!  ├──────────────────────┤
│    checksum=ffff     ◀──────────────▶    checksum=aaab    │
├──────────────────────┤              ├──────────────────────┤
│    checksum=abab     │              │    checksum=abab     │
├──────────────────────┤              ├──────────────────────┤
│    checksum=f0f0     │              │    checksum=f0f0     │
├──────────────────────┤              ├──────────────────────┤
│         ...          │              │         ...          │
├──────────────────────┤              ├──────────────────────┤
│    checksum=9494     │              │    checksum=9494     │
└──────────────────────┘              └──────────────────────┘
```

Now **data-diff** will do exactly as it just did for the _whole table_ for only
this segment: Split it into `--bisection-factor` segments.

However, this time, because each segment has `100k/10=10k` entries, which is
less than the `--bisection-threshold`, it will pull down every row in the segment
and compare them in memory in **data-diff**.

```
┌──────────────────────┐              ┌──────────────────────┐
│     PostgreSQL       │              │      Snowflake       │
├──────────────────────┤              ├──────────────────────┤
│    id=100k..110k     │              │    id=100k..110k     │
├──────────────────────┤              ├──────────────────────┤
│    id=110k..120k     │              │    id=110k..120k     │
├──────────────────────┤              ├──────────────────────┤
│    id=120k..130k     │              │    id=120k..130k     │
├──────────────────────┤              ├──────────────────────┤
│    id=130k..140k     │              │    id=130k..140k     │
├──────────────────────┤              ├──────────────────────┤
│         ...          │              │         ...          │
├──────────────────────┤              ├──────────────────────┤
│      190k..200k      │              │      190k..200k      │
└──────────────────────┘              └──────────────────────┘
```

Finally **data-diff** will output the `(id, updated_at)` for each row that was different:

```
(122001, 1653672821)
```

If you pass `--stats` you'll see stats such as the % of rows were different.

### Performance Considerations

* Ensure that you have indexes on the columns you are comparing. Preferably a
  compound index. You can run with `--interactive` to see an `EXPLAIN` for the
  queries.
* Consider increasing the number of simultaneous threads executing
  queries per database with `--threads`. For databases that limit concurrency
  per query, such as PostgreSQL/MySQL, this can improve performance dramatically.
* If you are only interested in _whether_ something changed, pass `--limit 1`.
  This can be useful if changes are very rare. This is often faster than doing a
  `count(*)`, for the reason mentioned above.
* If the table is _very_ large, consider a larger `--bisection-factor`. Otherwise, you may run into timeouts.
* If there are a lot of changes, a larger `--bisection-threshold` may help, but on Db2, SQL Server and SAP ASE against PostgreSQL the default (16,384) was the best setting we measured.
* If there are very large gaps in your key column (e.g., 10s of millions of
  continuous rows missing), then **data-diff** may perform poorly, doing lots of
  queries for ranges of rows that do not exist. This isn't addressed (see below). If you're experiencing this effect, please open an issue, and we
  will prioritize it.
* The fewer columns you verify (passed with `--columns`), the faster
  **data-diff** will be. On one extreme, you can verify every column; on the
  other, you can verify _only_ `updated_at`, if you trust it enough. You can also
  _only_ verify `id` if you're interested in only presence, such as to detect
  missing hard deletes. You can do also do a hybrid where you verify
  `updated_at` and the most critical value, such as a money value in `amount`, but
  not verify a large serialized column like `json_settings`.
* The Python API (`diff_tables()`) now sizes its thread pool from `thread_count` when you don't pass
  `max_threadpool_size`, as the CLI does. It used to default to 1, so segments ran one at a time.

### Tuning on the `perf/tuning` branch

Measured on the matched rig (4 pinned vCPUs and 4 GB per database, 4 threads), with Db2, SQL Server and SAP ASE
compared to PostgreSQL on `fact_2x`, `fact_13x` and `oltp_order_line`. See the README for the timings.

**Implemented**

* **A thread pool derived from `thread_count`.** Before it, one `fact_13x` diff spent all its wall time in one
  query at a time. The other two changes don't touch ASE `fact_13x` identical, which went from 20.6 s to 7.2 s, so the pool
  is the likely cause. No change was measured alone, so the share of each isn't known.
* **A bounded hash input on SQL Server.** Text declared up to 4,000 long is hashed as `NVARCHAR(n)` instead of
  `NVARCHAR(MAX)`, and non-text columns are hashed without a conversion. One checksum of the whole table went from
  29.9 s to 20.3 s on `oltp_order_line` and from 20.6 s to 15.5 s on `fact_13x`, with identical values. It's used
  only when the declared lengths prove the bound (2,666 UTF-16 units), because the bounded form truncates silently
  beyond it. `varchar(8000)` columns still use `MAX`, and so does `numeric`, which SQL Server's type map doesn't
  know: that's a possible follow-up, not measured.
* **A row hash in the leaf for Db2 and PostgreSQL.** Below `bisection_threshold` the segment's rows used to be
  downloaded. Now each row's key, hash and NULL mask are read, and only the differing rows are fetched. Db2
  downloads cost 9 times PostgreSQL's, so Db2 gained the most: `oltp_order_line` updated went from 18.0 s to 4.7 s.
  Its blind spot is text with `|` that moves between neighbouring columns, which checksums share. When most of a
  leaf differs, or its keys can't be fetched, it downloads every row as before.

**Not implemented, and why**

* **A faster hash than MD5.** Not tried: SQL Server's SHA1 cost the same as MD5 in a probe (0.291 against
  0.293 s), no function exists on every database, and it would change every checksum. On SQL Server the cost was
  the `MAX` types, not the hash.
* **A dynamic `bisection_factor`.** Factors of 8 and 128 were measured against 32 in serial runs: 8 was within
  noise or worse (up to +25%) and 128 was worse everywhere (up to +75%). Not retried after the other changes.
* **A larger bisection threshold per database.** Phase 1's serial runs found SQL Server 33-47% faster at 262,144
  and ASE 9-14% faster. After the changes above, a sweep of 16k to 512k on all three pairs found nothing
  faster beyond twice the noise on every table and scenario, and 262,144 and above cost up to +405% (ASE,
  `oltp_order_line`, clustered changes). SQL Server's checksums got cheaper, up to 8 queries run at once, and Db2
  leaves are cheap, so the old optimum is gone. 16,384 stays. A threshold below that was measured only in the
  serial runs: 1,024 cost +12% to +430%.
* **Large key gaps.** Not measured. It was assumed to cost under 2% on these tables, whose keys have no big gaps.
* **Bypassing the driver for Db2 downloads.** The estimate was 4.1%, under the 14% a change had to promise, and
  it wasn't built.
* **Per-connection engine settings.** The rig already uses each engine at its measured best, so there was nothing
  to gain there.
* **Runtime adaptation of any of these.** A checksum can't say how many rows changed, so a threshold chosen at run
  time would be a guess. The sweep found the best threshold didn't move by more than a factor of 4 with the density
  of changes (16,384 to 131,072 were flat).

Not tested: databases other than these four, other row shapes, a real network between data-diff and the databases,
and comparisons that aren't against PostgreSQL.

### Tried: reducing type-casts in checksums (not merged)

We shortened the checksum SQL for Db2 and SAP ASE: Db2 took the MD5 tail with `TRANSLATE`, rendered `DATE` with
`CHAR(x, ISO)` and coalesced each column once. Both skipped `ROUND` and the decimal cast when a `DECIMAL` or `numeric`
already had the compared scale. All 1592 tests passed, and the checksums were identical to master on PostgreSQL, Db2
and ASE.

The checksum alone got faster on `fact_13x`: Db2 10.3-10.7 s to 8.55 s, ASE 14.5 s to 13.5 s. In full diffs, which also
bisect and download rows, the median of 3 alternating runs against master moved by 0-5% for Db2, SQL Server and ASE
to PostgreSQL on `fact_2x` (1.55M rows) and `fact_13x` (10.09M rows). Run-to-run noise on this machine is about 20%, so
this is not a result, and we stopped. SQL Server, where the change did nothing, was flat as expected.

The work is kept at the tag `archive/perf-fewer-casts` (6 commits). Not tried: Db2 renders integers as
`VARCHAR(VARCHAR(x))`, a double cast. Db2 `DATE` against PostgreSQL `TIMESTAMP(0)` at midnight gives different
checksums, and MySQL renders `DATE` with a time part. Both differences were seen on the branch and not checked on master.
