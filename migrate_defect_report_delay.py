"""Add cached survey-date and defect-email tracking fields to surveys.

Safe to re-run. Run with DATABASE_URL configured before using the new admin page.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS extracted_survey_end_date DATE",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS survey_end_date_confidence DOUBLE PRECISION",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS defect_report_sent_at TIMESTAMP",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS defect_report_sent_confidence DOUBLE PRECISION",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS defect_report_email_id VARCHAR(255)",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS defect_report_match_status VARCHAR(30)",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS defect_report_delay_days INTEGER",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete.")