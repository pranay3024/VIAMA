import requests

from app import app
from models.db_models import Survey
from extensions import db

from google_drive import upload_file_to_drive

PDF_FOLDER_ID = "1gkUDQh63qeSkJzZa6SLbB1LD06hEm1G7"

with app.app_context():

    surveys = Survey.query.all()

    total = len(surveys)

    print(f"Found {total} surveys")

    migrated = 0

    for survey in surveys:

        if not survey.end_survey_pdf:
            continue

        if "drive.google.com" in survey.end_survey_pdf:
            continue

        try:

            print(f"Migrating Survey {survey.id}")

            response = requests.get(
                survey.end_survey_pdf,
                timeout=60
            )

            response.raise_for_status()

            filename = (
                f"Section_{survey.section_no}"
                f"_Cycle{survey.cycle_no}.pdf"
            )

            result = upload_file_to_drive(
                file_bytes=response.content,
                filename=filename,
                folder_id=PDF_FOLDER_ID,
                mime_type="application/pdf"
            )

            survey.end_survey_pdf = result["view_url"]

            db.session.commit()

            migrated += 1

            print("✓ Done")

        except Exception as e:

            print("ERROR:", survey.id, e)

    print(f"\nMigrated {migrated} PDFs")