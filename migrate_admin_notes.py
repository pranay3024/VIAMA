"""Create the table that keeps each admin's delayed-reports notepad."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    """
    CREATE TABLE IF NOT EXISTS admin_notes (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL UNIQUE,
        body TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))

print("Migration complete: admin_notes")
