### ⚠️ As of May 17, 2024, Datafold is no longer actively supporting or developing open source data-diff. We’re grateful to everyone who made contributions along the way. Please see [our blog post](https://www.datafold.com/blog/sunsetting-open-source-data-diff) for additional context on this decision.

---

# data-diff: Compare datasets fast, within or across SQL databases

## About this fork

This fork adds IBM Db2 and SAP Sybase ASE support, and is maintained to compare data between Db2 12.1, SQL Server 2025, PostgreSQL 18 and SAP ASE 16.1. Releases are published here as GitHub Releases, not on PyPI.

## Install

You need Python 3.9 or later, and:

- [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) on hosts that connect to SQL Server.
- The FreeTDS ODBC driver on hosts that connect to SAP ASE: `sudo apt-get install tdsodbc` on Debian and Ubuntu, or `freetds` from EPEL on RHEL. It must be registered under the name `FreeTDS`, which the Debian package does. For another name, pass `odbc_driver="..."`.
- On Linux, glibc 2.34 or later for the Db2 driver.

```shell
pip install "data-diff[db2,mssql,postgresql,sybase] @ https://github.com/bill-ramos-rmoswi/data-diff/releases/download/v0.14.0/data_diff-0.14.0-py3-none-any.whl"
```

## Compare tables

```python
from data_diff import connect_to_table, diff_tables

source = connect_to_table(
    "postgresql://user:password@host:5432/database",
    "fact",
    ("id",),
    extra_columns=("amount", "updated_on"),
)
target = connect_to_table(
    "db2://user:password@host:50000/DATABASE/SCHEMA",
    "FACT",
    ("ID",),
    extra_columns=("AMOUNT", "UPDATED_ON"),
)

try:
    for sign, row in diff_tables(source, target):
        print(sign, row)  # "-" for a row in source, "+" for a row in target
finally:
    source.database.close()
    target.database.close()
```

An empty result means the compared columns match. Only the key columns and `extra_columns` are compared.

A SQL Server URI looks like `mssql://user:password@host:1433/database/schema`. Add `?TrustServerCertificate=yes` if the server uses a self-signed certificate.

An SAP ASE URI looks like `sybase://user:password@host:5000/database/owner`, where the owner defaults to `dbo`. A table can be named as `table`, `owner.table` or `database.owner.table`.

URIs aren't decoded, so a password containing `@`, `/` or `#` won't work in one, even when it's URL-encoded. Pass a dict instead, which also keeps the password out of the URI:

```python
source = connect_to_table(
    dict(
        driver="mssql",
        host="host",
        port=1433,
        user="user",
        password="P@ss/w0rd#1",
        database="database",
        schema="dbo",
        TrustServerCertificate="yes",  # only for a self-signed certificate
    ),
    "fact",
    ("id",),
    extra_columns=("amount", "updated_on"),
)
```

For Db2, use `driver="db2"` and `port=50000`, and leave out `TrustServerCertificate`. For SAP ASE, use `driver="sybase"`, `port=5000` and the table owner as `schema`, such as `dbo`.

Things to check before your first run:

- Db2 table and column names are case-sensitive, and Db2 stores unquoted names in uppercase.
- Db2 `DECIMAL` columns hold up to 31 digits.
- A row of compared Db2 `VARCHAR` text can't be longer than 32,672 bytes, which is the most its `HASH` function takes. The diff stops with an error, so remove long columns from `extra_columns`. `CLOB`, `DBCLOB` and `BLOB` columns have no such limit, because they're compared by a digest, see [Large text and binary values](#large-text-and-binary-values).
- Float values with more than 15 significant digits can show as different against PostgreSQL, which keeps only 15 when it converts a float to a decimal. Floats of 1E25 or more can't be compared across databases at the default precision.
- SQL Server `uniqueidentifier` keys compare more slowly, because those filters can't use an index.
- Checksums scan the tables you compare, so start with a small table, off-peak.

## SAP ASE

data-diff connects to ASE through pyodbc and FreeTDS, using TDS 5.0. It needs a server with the cp850 character set and a binary sort order, and it refuses others when it connects. It was tested on ASE 16.1 SP00 PL02 with 4 KB pages, cp850 and `bin_cp850`.

To compare databases, every database must hash the same bytes for equal values. ASE can't convert text to UTF-8 on the server, so data-diff builds the UTF-8 bytes in SQL. That's why some limits below exist.

### How data types differ between databases

These were measured on PostgreSQL 18.6, SQL Server 2025, Db2 12.1 and ASE 16.1. data-diff normalizes each value before comparing, so the differences matter when a copy between databases changed a value, and when a value can't be represented the same way.

| Behavior | PostgreSQL | SQL Server | Db2 | SAP ASE |
| --- | --- | --- | --- | --- |
| Cast a decimal to fewer digits (1.125 to 2 places) | Rounds: 1.13 | Rounds: 1.13 | Truncates: 1.12 | Truncates: 1.12 |
| Insert more decimals than the column holds | Rounds | Rounds | Truncates | Error 241. `money` rounds |
| `money` | No | 4 decimals | No | 4 decimals, but shown with 2 |
| Float to decimal | 15 significant digits | Exact binary value | Shortest form that converts back | data-diff matches PostgreSQL |
| Timestamp precision | Microseconds | `datetime2`: 100 ns. `datetime`: 1/300 s | Up to 12 digits | `bigdatetime`: µs. `datetime`: 1/300 s, shown truncated to ms. `smalldatetime`: minutes |
| Time precision | Microseconds | 100 ns | Seconds | `bigtime`: µs. `time`: 1/300 s |
| More than 6 fractional digits in a literal | Rounds | Rounds | Truncates | Error 249 |
| Empty string `''` | `''` | `''` | `''` | Stored as one space |
| Trailing spaces in `varchar` | Kept | Kept | Kept | Removed. `text` keeps them |
| `char` padding, as data-diff compares it | Removed | Kept | Kept | Removed. ASE pads only NOT NULL columns |
| Boolean | `boolean` | `bit` | `BOOLEAN` | `bit`, which can't be NULL |
| Unicode text | Any text type | `nvarchar` | `VARCHAR` (UTF-8) | Only `univarchar`, `unichar` and `unitext`. `varchar` holds cp850 |
| Text sort order | Collation, often ignoring case | Collation, often ignoring case | Binary | Binary, in cp850 byte order |
| Longest text hashed per row | Not measured | Not measured | 32,672 bytes | 16,384 bytes of UTF-8 |
| Full outer join, for joindiff | Yes | Yes | Yes | No, so data-diff joins left and right and combines them |

### Limits on ASE

`text`, `unitext` and `image` columns are compared by a digest, see [Large text and binary values](#large-text-and-binary-values). These limits fail with an error that names them, rather than compare wrongly:

- A row of compared `varchar` and `univarchar` text over 16,384 bytes of UTF-8. ASE truncates longer expressions without an error.
- A `univarchar` or `unichar` value with characters outside cp850 (such as CJK or emoji) that's longer than 255 characters. Text that fits in cp850 has no such limit. Each such character adds about 1.6 KB of SQL, which uses ASE's procedure cache, and data-diff retries when ASE reports that it's full.
- With the default algorithm, a `text` value over 4,198,400 characters, an `image` value over 8,396,800 bytes, a `unitext` value over 4,096 characters, or a `unitext` value over 128 characters with characters outside cp850. The `rowhash` algorithm compares all of these.
- With joindiff, a `text` or `unitext` value over 16,384 bytes.

These are real differences in the stored values, and they're reported:

- `''` on another database against ASE's single space.
- Trailing spaces in `varchar` and `univarchar`, which ASE removes when it stores a value.
- Characters outside cp850 in a `varchar` column on another database. They can't be stored in ASE `varchar`, so use `univarchar` for them.

Other differences to know:

- `datetime` and `time` hold 1/300 second steps, and ASE shows them truncated to milliseconds. data-diff compares them at 3 digits using their exact value, which matches SQL Server's `datetime` and copies made through ASE. Against another 3-digit column, make the ASE table the first one in `diff_tables()`, because data-diff uses the first table's rounding when both have the same precision.
- `char` columns compare without padding on ASE and PostgreSQL, and with it on SQL Server and Db2, so a `char` column there differs from ASE unless the value fills it.
- Floats with more than 15 significant digits match PostgreSQL, but can differ against SQL Server and Db2, which convert them differently.
- Keys can be integers (including `unsigned` types and `numeric(n, 0)`), alphanumeric text or UUID text, as on other databases. `timestamp` columns can't be compared, because a copy of a row doesn't keep its row version.
- SQL text is sent in cp850, so data-diff writes other characters as `U&''` escapes. FreeTDS returns `unitext` as UTF-16LE bytes, so joindiff's materialized tables, which use `unitext`, read back as bytes through pyodbc.
- A read-only login needs only `SELECT` on the compared tables.

## Large text and binary values

SAP ASE hashes at most 16,384 bytes at a time and Db2 at most 32,672, and their large types hold up to 2 GB. So data-diff compares these columns by a digest that every database computes in the same chunks, instead of by value:

- The digest is the sum, over 4,096-character chunks of text or 8,192-byte chunks of bytes, of the last 12 hex digits of `md5(<chunk number>|<chunk>)`. Text is hashed as UTF-8. `data_diff.utils.lob_digest()` computes the same number in Python.
- It applies to ASE `text`, `unitext` and `image`, Db2 `CLOB`, `DBCLOB` and `BLOB`, and every binary column (`binary`, `varbinary`, `bytea`), together with whatever column they're paired with on the other side.
- Bytes are hashed inside each database, so no driver renders them as hex, and prefixes such as `0x` and `\x` never reach a comparison.
- The rows that data-diff reports hold the digest of such a column, not its value.
- An empty value has the digest `0`, and NULL stays NULL.

SQL Server needs 2022 or later for these columns, because the digest uses `GENERATE_SERIES`.

### Reading each table once

The default algorithm, hashdiff, checksums a table, and when the checksums differ, checksums smaller and smaller segments of it. Each level reads the differing parts again, and hashes their large values again. It also can't use values that a database is unable to hash.

`algorithm=Algorithm.ROWHASH` (`--algorithm rowhash` on the command line) reads each table once. Each database returns the key and a hash of every row, data-diff compares the hashes, and only the rows whose hashes differ are read again, by key. A large value that a database can't hash is downloaded and hashed by data-diff:

| Database | Hashed by the database | Left to data-diff, with `rowhash` only |
| --- | --- | --- |
| SAP ASE | `text` to 4.2M characters, `image` to 8.4 MB, `unitext` to 4,096 characters in cp850 | Longer values, and `unitext` outside cp850 over 128 characters |
| PostgreSQL | Everything | Text over 1M characters that isn't ASCII, which PostgreSQL takes about 1 second per 2 MB to cut, and more for longer text |
| SQL Server | Everything | Nothing |
| Db2 | To 134M characters or 268 MB. Text that isn't ASCII takes about 4 seconds per MB | Longer values |

`RowHashDiffer(recheck_delay=60)` reads the differing rows a second time after that many seconds, and reports only those that still differ. Use it for a target that's still receiving changes.

Use hashdiff for tables that are expected to match and have no large values, because it sends nothing but checksums. Times in seconds for SAP ASE against PostgreSQL, on the tables described below:

| Algorithm | `fact_2x`, 1.55M rows: identical / updated / deleted | `fact_13x`, 10.09M rows: identical / updated / deleted |
| --- | --- | --- |
| hashdiff | 4.4 / 6.9 / 6.9 | 25.9 / 47.1 / 49.1 |
| rowhash | 7.0 / 6.8 / 6.8 | 62.1 / 66.5 / 65.9 |

## Performance against PostgreSQL

These timings simulate comparing an on-premises database with Aurora PostgreSQL, using PostgreSQL 18.6 in place of Aurora. Each source table is compared with a PostgreSQL copy holding the same rows. The tables are `FactProductInventory` from Microsoft's AdventureWorksDW2022, enlarged by `dev/perf/seed.py`, with a three-column integer key and five compared columns.

Times are in seconds, for a whole `diff_tables()` call with 4 threads per database:

| Source → PostgreSQL | `fact_2x`, 1.55M rows: identical / updated / deleted | `fact_13x`, 10.09M rows: identical / updated / deleted |
| --- | --- | --- |
| SAP ASE 16.1 | 3.5 / 6.7 / 6.7 | 21.8 / 45.2 / 45.7 |
| Db2 12.1 | 4.4 / 11.8 / 13.1 | 33.1 / 65.8 / 63.5 |
| SQL Server 2025 | 21.3 / 29.4 / 29.4 | 33.1 / 76.4 / 78.2 |

- **identical**: the tables match.
- **updated**: `UnitCost` changed on about 0.016% of the PostgreSQL rows (242 in `fact_2x`, 1,573 in `fact_13x`), spread across the table.
- **deleted**: the same rows deleted from PostgreSQL.

Every run reported exactly the rows that were changed. All four databases ran in containers on one workstation (16 cores, 24 GB), with SAP ASE at its 2 GB defaults (1.5 GB max memory, 768 MB data cache) and Db2 Community Edition limited to 4 cores. These numbers compare sources with each other, rather than predict production times: a real run also depends on the network between the host running data-diff and each database, and on Aurora's instance size. Times vary by about 20% from one run to the next.

## TODO: check the target environment

These pre-deployment checks were skipped and **haven't been done**. Read-only accounts were tested on local containers, but nothing below has been confirmed on a real target.

- [ ] Choose the host that runs data-diff, and confirm it has Python 3.9 or later and ODBC Driver 18. On Linux, also confirm glibc 2.34 or later.
- [ ] Create read-only accounts with `SELECT` on the compared tables, and `CONNECT` on Db2. PostgreSQL also needs `USAGE` on a schema other than `public`.
- [ ] Confirm SQL Server is 2019 or later, because hashing needs a UTF-8 collation. Only 2025 CU9 has been tested.
- [ ] Confirm PostgreSQL isn't in FIPS mode, because hashing needs `md5()`.
- [ ] Confirm the exact case of Db2 table and column names in `SYSCAT.COLUMNS`.
- [ ] Confirm each compared table has a unique key with integer, UUID or alphanumeric values.
- [ ] Confirm compared Db2 `VARCHAR` text stays under 32,672 bytes per row. `CLOB` columns have no such limit.
- [ ] Confirm the host can reach every database port.
- [ ] Confirm each ASE server uses cp850 and a binary sort order (`sp_helpsort`). data-diff refuses other servers when it connects.
- [ ] Install FreeTDS (`tdsodbc`) on the host that runs data-diff, and check that `odbcinst -q -d` lists `FreeTDS`.
- [ ] Confirm compared ASE `varchar` text stays under 16,384 bytes per row, and that `univarchar` text with characters outside cp850 stays under 255 characters.
- [ ] Find the largest `text`, `unitext` and `image` values in the ASE tables. Over the limits in [Reading each table once](#reading-each-table-once), compare with `rowhash`.
- [ ] Confirm SQL Server is 2022 or later if binary or large text columns are compared.
- [ ] Test the column types of the first tables you compare on the local containers.

## Contributors

<a href="https://github.com/datafold/data-diff/graphs/contributors">
  <img src="https://contributors-img.web.app/image?repo=datafold/data-diff" />
</a>

## License

This project is licensed under the terms of the [MIT License](https://github.com/datafold/data-diff/blob/master/LICENSE).
