"""Create the table that keeps every reschedule reason of a survey."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    """
    CREATE TABLE IF NOT EXISTS reschedule_reasons (
        id SERIAL PRIMARY KEY,
        survey_id INTEGER NOT NULL REFERENCES surveys(id),
        reason TEXT NOT NULL,
        rescheduled_to DATE,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_reschedule_reasons_survey_id ON reschedule_reasons (survey_id)",
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))

print("Migration complete: reschedule_reasons")
