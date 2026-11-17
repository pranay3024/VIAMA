"""Add the reschedule date and the reschedule mail thread id to surveys."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS reschedule_to_date DATE",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS reschedule_thread_message_id VARCHAR(255)",
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))

print("Migration complete: reschedule_to_date, reschedule_thread_message_id")
