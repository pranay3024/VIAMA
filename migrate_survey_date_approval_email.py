"""Add survey date-approval and raw-video email tracking columns."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "survey_dates_approved BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "survey_dates_approved_at TIMESTAMP",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "raw_video_email_sent_at TIMESTAMP",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "raw_video_email_message_id VARCHAR(255)",
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete: survey date approval and email tracking")