"""Fail when the connected database is missing a table or column the app selects.

Why this exists
---------------
A deployment can be green and still crash on the first request:

    psycopg2.errors.UndefinedColumn: column surveys.defect_report_email_sent_at does not exist

The code selects a column, the database never got the migration, and every
page that touches that model returns 500.  The migration is a one-liner, but
running it against the *wrong* database fixes nothing - and there are two
Supabase projects in play (production and a report copy), so "the wrong one"
is easy to hit.

This script compares the schema the models expect against the schema the
target database actually has, and exits non-zero on any gap.

Usage
-----
    python check_database_schema.py
        Checks PRODUCTION_DATABASE_URL, falling back to DATABASE_URL.

    python check_database_schema.py --database-url postgresql://...
        Checks an explicit database.

    python check_database_schema.py --env-file .env
        Checks the DATABASE_URL from that file.

Exit codes
----------
    0  every table and column the models need exists
    1  something is missing (the list says what)
    2  the database could not be reached
"""

import argparse
import os
import re
import sys


def load_env_file(path):
    """Read KEY=VALUE pairs without overriding the real environment."""
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def resolve_url(args):
    if args.database_url:
        return args.database_url, "command line"

    file_values = load_env_file(args.env_file)

    for name in ("PRODUCTION_DATABASE_URL", "DATABASE_URL"):
        if os.environ.get(name):
            return os.environ[name], "environment variable " + name
        if file_values.get(name):
            return file_values[name], "%s in %s" % (name, args.env_file)

    raise SystemExit(
        "No database URL found. Set PRODUCTION_DATABASE_URL or DATABASE_URL, "
        "or pass --database-url."
    )


def masked(url):
    return re.sub(r"://[^@/]*@", "://***@", url)


def collect_expected_tables():
    """Every table the models declare, with their columns."""
    from extensions import db

    import models.db_models  # noqa: F401  (registers the portal tables)
    import core.models  # noqa: F401  (registers the API tables)

    expected = {}
    for table in db.metadata.tables.values():
        expected[table.name] = {column.name for column in table.columns}
    return expected


def collect_actual_columns(engine):
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select table_name, column_name from information_schema.columns "
                "where table_schema = 'public'"
            )
        ).fetchall()

    actual = {}
    for table_name, column_name in rows:
        actual.setdefault(table_name, set()).add(column_name)
    return actual


def compare(expected, actual):
    """Return (missing_tables, missing_columns) for the two schemas."""
    missing_tables = sorted(set(expected) - set(actual))
    missing_columns = []
    for table_name in sorted(set(expected) & set(actual)):
        for column in sorted(expected[table_name] - actual[table_name]):
            missing_columns.append((table_name, column))
    return missing_tables, missing_columns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="explicit database to check")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="env file to read DATABASE_URL / PRODUCTION_DATABASE_URL from",
    )
    args = parser.parse_args()

    url, source = resolve_url(args)
    print("Checking database:", masked(url))
    print("Resolved from     :", source)

    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"options": "-csearch_path=public"},
        )
        actual = collect_actual_columns(engine)
    except SQLAlchemyError as exc:
        print("Could not reach the database:", type(exc).__name__, exc)
        return 2

    expected = collect_expected_tables()

    missing_tables, missing_columns = compare(expected, actual)

    print("")
    print("Tables checked    :", len(expected))
    print("Tables missing    :", len(missing_tables))
    print("Columns missing   :", len(missing_columns))

    if not missing_tables and not missing_columns:
        print("")
        print("OK - the database has every table and column the models expect.")
        return 0

    if missing_tables:
        print("")
        print("Missing tables:")
        for table_name in missing_tables:
            print("  -", table_name)
        print("  run: python -m core.cli create-tables")

    if missing_columns:
        print("")
        print("Missing columns:")
        for table_name, column in missing_columns:
            print("  - %s.%s" % (table_name, column))
        print("  run the matching migrate_*.py against THIS database")

    print("")
    print("Migrations must be run against the database this script just")
    print("checked, not against whatever DATABASE_URL happens to point at.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
