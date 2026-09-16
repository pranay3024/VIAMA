"""Add the admin survey-form approval flag to surveys."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statement = text(
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "survey_form_approved BOOLEAN NOT NULL DEFAULT FALSE"
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    connection.execute(statement)

print("Migration complete: survey_form_approved")
