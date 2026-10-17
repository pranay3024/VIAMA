"""Create tables for the manually maintained weekly and cancellation reports.

Safe to re-run. Run with DATABASE_URL configured.

    python migrate_weekly_survey_reports.py
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS weekly_survey_plans (
        id SERIAL PRIMARY KEY,
        week_no INTEGER NOT NULL UNIQUE,
        north_east INTEGER NOT NULL DEFAULT 0,
        up_jh INTEGER NOT NULL DEFAULT 0,
        odisha INTEGER NOT NULL DEFAULT 0,
        conducted INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cancelled_survey_reports (
        id SERIAL PRIMARY KEY,
        week_no INTEGER NOT NULL,
        section_or_upc VARCHAR(150) NOT NULL,
        reason VARCHAR(40) NOT NULL,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_cancelled_survey_reports_week_no
    ON cancelled_survey_reports (week_no)
    """,
    "ALTER TABLE cancelled_survey_reports ADD COLUMN IF NOT EXISTS team VARCHAR(40) NOT NULL DEFAULT ''",
    "ALTER TABLE cancelled_survey_reports ADD COLUMN IF NOT EXISTS section_no VARCHAR(100)",
    "ALTER TABLE cancelled_survey_reports ADD COLUMN IF NOT EXISTS upc_code VARCHAR(100)",
    "ALTER TABLE weekly_survey_plans ADD COLUMN IF NOT EXISTS total_scheduled INTEGER",
    "UPDATE weekly_survey_plans SET total_scheduled = north_east + up_jh + odisha WHERE total_scheduled IS NULL",
    "ALTER TABLE weekly_survey_plans ADD COLUMN IF NOT EXISTS cancelled_count INTEGER",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", " ".join(statement.split())[:80])

print("Migration complete.")