FROM mcr.microsoft.com/devcontainers/python:3.11-bookworm

# ODBC Driver 18 and sqlcmd/bcp for SQL Server, FreeTDS for SAP ASE (TDS 5.0, with
# tsql and freebcp), plus client libraries for the other databases.
# ACCEPT_EULA accepts the Microsoft ODBC driver license.
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 mssql-tools18 unixodbc-dev libpq-dev postgresql-client \
        tdsodbc freetds-bin \
    && rm -rf /var/lib/apt/lists/*

# Poetry gets its own venv, so it can't conflict with project dependencies.
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --no-cache-dir poetry==1.8.5 \
    && ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

ENV PATH="${PATH}:/opt/mssql-tools18/bin"

# Named volumes inherit this ownership. Without it, Claude Code's config volume
# is created as root and the vscode user can't write to it.
RUN mkdir -p /home/vscode/.claude && chown -R vscode:vscode /home/vscode/.claude
