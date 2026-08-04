"""Add the automated video-count check columns to the surveys table.

Standalone on purpose - talks to DATABASE_URL directly rather than importing the
Flask app, so it runs without the full request-path dependency chain installed.

Safe to re-run: both statements use IF NOT EXISTS.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS video_count_matched BOOLEAN",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS video_count_checked_at TIMESTAMP",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:

    for statement in STATEMENTS:

        connection.execute(text(statement))

        print("OK:", statement)

    result = connection.execute(text(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_name = 'surveys' "
        "AND column_name IN ('video_count_matched', 'video_count_checked_at') "
        "ORDER BY column_name"
    )).fetchall()

print("\nVerified columns on surveys:")

for name, data_type in result:
    print("  {:<24} {}".format(name, data_type))

print("\nMigration complete.")
