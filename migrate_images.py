import re
import requests

from app import app
from extensions import db
from models.db_models import Survey

from google_drive import upload_file_to_drive

IMAGE_FOLDER_ID = "1W1SCf0_E28VdM8zfzzjFOR-aJL2-e0tT"

with app.app_context():

    surveys = Survey.query.all()

    total = len(surveys)
    migrated = 0
    skipped = 0
    failed = 0

    print(f"\nFound {total} surveys\n")

    for i, survey in enumerate(surveys, start=1):

        if not survey.dashcam_photo:
            skipped += 1
            continue

        # Already migrated
        if "drive.google.com" in survey.dashcam_photo:
            skipped += 1
            continue

        try:

            print(f"[{i}/{total}] Survey {survey.id}")

            response = requests.get(
                survey.dashcam_photo,
                timeout=60
            )

            response.raise_for_status()

            import mimetypes

            ext = ".jpg"

            content_type = "image/jpeg"

            if survey.dashcam_photo.lower().endswith(".png"):
             ext = ".png"
             content_type = "image/png"

            elif survey.dashcam_photo.lower().endswith(".jpeg"):
             ext = ".jpeg"
             content_type = "image/jpeg"

            elif survey.dashcam_photo.lower().endswith(".jpg"):
             ext = ".jpg"
             content_type = "image/jpeg"

            safe_section = re.sub(
                r'[^A-Za-z0-9_-]',
                '_',
                survey.section_no or str(survey.id)
            )

            filename = (
                f"Section_{safe_section}"
                f"_Cycle{survey.cycle_no}"
                f"_Dashcam{ext}"
            )

            result = upload_file_to_drive(
                file_bytes=response.content,
                filename=filename,
                folder_id=IMAGE_FOLDER_ID,
                mime_type=content_type)
            

            survey.dashcam_photo = result["image_url"]

            migrated += 1

            # Commit every 20 records
            if migrated % 20 == 0:
                db.session.commit()

            print("   ✅ Image migrated")

        except Exception as e:

            failed += 1

            print(f"   ❌ ERROR: {e}")

    db.session.commit()

    print("\n========================")
    print(f"Images Migrated : {migrated}")
    print(f"Skipped         : {skipped}")
    print(f"Failed          : {failed}")
    print("========================")