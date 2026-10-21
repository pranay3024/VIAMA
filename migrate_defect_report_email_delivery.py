"""Add separate delivery markers for RoadVision defect-report emails."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "defect_report_email_sent_at TIMESTAMP",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "defect_report_email_message_id VARCHAR(255)",
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete: defect-report email delivery markers")