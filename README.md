### ⚠️ As of May 17, 2024, Datafold is no longer actively supporting or developing open source data-diff. We’re grateful to everyone who made contributions along the way. Please see [our blog post](https://www.datafold.com/blog/sunsetting-open-source-data-diff) for additional context on this decision.

---

# data-diff: Compare datasets fast, within or across SQL databases

## About this fork

This fork adds IBM Db2 support and is maintained to compare data between Db2 12.1, SQL Server 2025 and PostgreSQL 18. Releases are published here as GitHub Releases, not on PyPI.

## Install

You need Python 3.9 or later, and [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) on hosts that connect to SQL Server. On Linux, the Db2 driver needs glibc 2.34 or later.

```shell
pip install "data-diff[db2,mssql,postgresql] @ https://github.com/bill-ramos-rmoswi/data-diff/releases/download/v0.12.0/data_diff-0.12.0-py3-none-any.whl"
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

For Db2, use `driver="db2"` and `port=50000`, and leave out `TrustServerCertificate`.

Things to check before your first run:

- Db2 table and column names are case-sensitive, and Db2 stores unquoted names in uppercase.
- Db2 `DECIMAL` columns hold up to 31 digits.
- Db2 can't compare text longer than 32,672 bytes, or a row of compared text longer than that, because its `HASH` function doesn't accept a `CLOB`. The diff stops with an error, so remove long columns from `extra_columns`.
- Float values with more than 15 significant digits can show as different against PostgreSQL, which keeps only 15 when it converts a float to a decimal. Floats of 1E25 or more can't be compared across databases at the default precision.
- SQL Server `uniqueidentifier` keys compare more slowly, because those filters can't use an index.
- Checksums scan the tables you compare, so start with a small table, off-peak.

## TODO: check the target environment

These pre-deployment checks were skipped and **haven't been done**. Read-only accounts were tested on local containers, but nothing below has been confirmed on a real target.

- [ ] Choose the host that runs data-diff, and confirm it has Python 3.9 or later and ODBC Driver 18. On Linux, also confirm glibc 2.34 or later.
- [ ] Create read-only accounts with `SELECT` on the compared tables, and `CONNECT` on Db2. PostgreSQL also needs `USAGE` on a schema other than `public`.
- [ ] Confirm SQL Server is 2019 or later, because hashing needs a UTF-8 collation. Only 2025 CU9 has been tested.
- [ ] Confirm PostgreSQL isn't in FIPS mode, because hashing needs `md5()`.
- [ ] Confirm the exact case of Db2 table and column names in `SYSCAT.COLUMNS`.
- [ ] Confirm each compared table has a unique key with integer, UUID or alphanumeric values.
- [ ] Confirm compared Db2 text stays under 32,672 bytes per row.
- [ ] Confirm the host can reach all three database ports.
- [ ] Test the column types of the first tables you compare on the local containers.

## Contributors

<a href="https://github.com/datafold/data-diff/graphs/contributors">
  <img src="https://contributors-img.web.app/image?repo=datafold/data-diff" />
</a>

## License

This project is licensed under the terms of the [MIT License](https://github.com/datafold/data-diff/blob/master/LICENSE).
