# Changelog

This fork of data-diff (`bill-ramos-rmoswi/data-diff`) is released as a wheel attached to each [GitHub Release](https://github.com/bill-ramos-rmoswi/data-diff/releases). Each version's section here is that release's description. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.16.0] - 2026-11-14

Diffs from Db2, SQL Server and SAP ASE to PostgreSQL are faster than in 0.14.0, and two correctness gaps are closed. No checksum value changed.

### Added

- Each release attaches `SHA256SUMS`, an SPDX SBOM (`sbom.spdx.json`) and a signed build-provenance attestation. See "Verifying a release" in the README.
- Each pull request builds the wheel, installs it with its database extras in a clean virtual environment, and runs a diff with it. The release runs the same check on the files it publishes.

### Fixed

- **SQL Server `numeric` columns are typed as `Decimal`**, like `decimal`. They were `UnknownColType`, so a diff into SQL Server with a `numeric` column raised `TypeError`, a `numeric` key raised `NotImplementedError`, and a lower scale on the other side showed false differences. The "no compatibility handling" warning for these columns is gone. This isn't a speed change.
- **Db2 and PostgreSQL leaves tell `'a|b','c'` from `'a','b|c'`.** Leaf row hashes length-prefix text columns that can hold `|`, so a moved separator is reported when its segment is compared by leaf. When the hash input of a Db2 row would pass 32,672 bytes (SQL0137N), the leaf downloads every row. Each leaf query costs 4-7% more, within the measured noise.
- **The CLI failed at import on a clean install of 0.15.0** (`PydanticUserError: regex is removed`). pip installed pydantic 2, and the dbt and Datafold modules use the pydantic 1 API. The dependency is now `pydantic>=1.10.12,<2`. The Python API wasn't affected.
- **`--bisection-factor` and `--bisection-threshold` crashed hashdiff** with `TypeError: '>=' not supported between instances of 'int' and 'str'`, because the CLI passed them as text. Both are now integers. This came from upstream data-diff.

### Performance

Full `diff_tables()` calls to PostgreSQL on the matched rig (4 pinned vCPUs and 4 GB per database, `N_THREADS=4`), 0.14.0 (`7f4ad77`) against `c1724ba`, median of 5 alternating runs from `dev/perf/ab.py`, median noise 6%. 26 of 27 combinations of `fact_2x`, `fact_13x` and `oltp_order_line` with identical, updated and deleted rows are faster, by 30.5% to 72.8% beyond twice their noise. Db2 `fact_2x` identical had 27% noise, so it isn't a result. The README has the table. Most of this is 0.15.0's work, measured now against 0.14.0 with the same rig; F1 and F2 add no measured gain.

### Compatibility

- **Checksums:** no checksum value changed. Only a false difference at a lower `numeric` scale goes away.
- **Types:** SQL Server `numeric(p,s)` is `Decimal(precision=s)` instead of `UnknownColType`.
- **Tested with** PostgreSQL 18.6, SQL Server 2025 CU9, Db2 12.1.5.0, SAP ASE 16.1 SP00 PL02 and MySQL 8.4.13, on Python 3.9, 3.10 and 3.11.
- **Upgrade note:** code that caught `TypeError` or `NotImplementedError` from `numeric` columns in SQL Server no longer sees them.
- **Known limit:** the moved `|` is still missed in checksums and in `RowHashDiffer`, which keep their row hash, so a segment above `bisection_threshold` whose only difference is a moved `|` isn't flagged.

## [0.15.0] - 2026-11-13

Faster diffs from Db2, SQL Server and SAP ASE to PostgreSQL. No checksum value changed.

### Changed

- **`diff_tables()` runs segments in parallel by default.** When `max_threadpool_size` isn't passed, it's now `max(2, 2 * max(thread_count))`, as the CLI has always used. It was 1, so segments ran one at a time. An explicit value still wins, `None` included, and `1` gives the old serial order. Results can arrive in a different order, so compare them sorted.
- **Db2 and PostgreSQL compare small segments by row hash.** Below `bisection_threshold`, hashdiff reads each row's key, hash and NULL mask, and fetches only the rows that differ. It falls back to downloading every row when:
  - the fetched rows don't match the keys
  - a key can't be parsed
  - more than half the rows, and more than 500, differ

### Performance

- **SQL Server hashes declared-length text as `NVARCHAR(n)` instead of `NVARCHAR(MAX)`.** This applies when the declared lengths prove the value fits (2,666 UTF-16 units), and non-text columns skip the conversion. A whole-table checksum went from 29.9 s to 20.3 s on `oltp_order_line`, and from 20.6 s to 15.5 s on `fact_13x`.
- **Full diffs to PostgreSQL are 31–76% faster** on the matched benchmark rig (4 pinned vCPUs and 4 GB per database, `N_THREADS=4`). That covers 26 of 27 combinations of Db2, SQL Server and ASE with `fact_2x`, `fact_13x` and `oltp_order_line` and with identical, updated and deleted rows, each beyond twice its measured noise. See the README for the table.

### Fixed

- `tests/test_sybase.py` closed a connection shared by two parallel diffs before both had finished.

### Compatibility

- **Checksums:** no checksum value changed. This was checked on PostgreSQL, Db2, SQL Server and ASE for `fact_13x` and `oltp_order_line`. Diffs between 0.14.0 and 0.15.0 on different sides are consistent.
- **Tested with** PostgreSQL 18.6, SQL Server 2025 CU9, Db2 12.1.5.0, SAP ASE 16.1 SP00 PL02 and MySQL 8.4.13, on Python 3.9, 3.10 and 3.11.
- **Upgrade note:** `connect_to_table()` returns one shared connection per URI and thread count. With parallel segments, code that closes it in each of several concurrent diffs can fail with "cannot schedule new futures after shutdown". Close it once, after every diff has finished.
- **Known limit:** text with `|` that moves between neighbouring columns (`'a|b','c'` against `'a','b|c'`) has the same row hash, in checksums and in leaf row hashes.

## [0.14.0] - 2026-11-11

### Added

- Large values (LOBs and binary columns) are compared by a chunked MD5 digest (`lob_digest`) on every database, instead of by value.
- `RowHashDiffer` (`algorithm=rowhash`) reads each table once, works across databases, and hashes values a database can't hash on the client.
- Benchmark timings for Db2, SQL Server and ASE to PostgreSQL in the README.

### Fixed

- CI falls back to `archive.ubuntu.com` when the runners' Azure mirror doesn't resolve.

## Earlier versions

Versions up to 0.13.0 are described in their [GitHub Releases](https://github.com/bill-ramos-rmoswi/data-diff/releases).

[Unreleased]: https://github.com/bill-ramos-rmoswi/data-diff/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/bill-ramos-rmoswi/data-diff/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/bill-ramos-rmoswi/data-diff/compare/v0.13.0...v0.14.0
