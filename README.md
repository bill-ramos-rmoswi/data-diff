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

Things to check before your first run:

- Db2 table and column names are case-sensitive, and Db2 stores unquoted names in uppercase.
- Db2 numbers are limited to 31 digits.
- SQL Server `uniqueidentifier` keys compare more slowly, because those filters can't use an index.
- Checksums scan the tables you compare, so start with a small table, off-peak.

## Contributors

<a href="https://github.com/datafold/data-diff/graphs/contributors">
  <img src="https://contributors-img.web.app/image?repo=datafold/data-diff" />
</a>

## License

This project is licensed under the terms of the [MIT License](https://github.com/datafold/data-diff/blob/master/LICENSE).
