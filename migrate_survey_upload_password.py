"""Create the Krish-team survey-form upload password table."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

create_table = text(
    "CREATE TABLE IF NOT EXISTS survey_upload_passwords ("
    "id SERIAL PRIMARY KEY, "
    "team VARCHAR(50) NOT NULL UNIQUE, "
    "password VARCHAR(255) NOT NULL, "
    "updated_by_email VARCHAR(150), "
    "updated_at TIMESTAMP"
    ")"
)

seed_row = text(
    "INSERT INTO survey_upload_passwords (team, password, updated_at) "
    "VALUES ('krish', :password, NOW()) "
    "ON CONFLICT (team) DO NOTHING"
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    connection.execute(create_table)
    connection.execute(seed_row, {"password": "VIAMA@Krish"})

print("Migration complete: survey_upload_passwords")
