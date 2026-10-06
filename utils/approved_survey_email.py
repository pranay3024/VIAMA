"""Send the approved survey-form email once both approvals are recorded."""

from datetime import datetime, timezone

from core.config import current_week_number, week_of
from extensions import db
from google_drive import download_file_from_drive, send_gmail_email
from models.db_models import Survey
from utils.email_templates import build_email_body, build_subject


TO_EMAIL = "ishit1561@gmail.com"
FROM_EMAIL = "adordashcam@gmail.com"


def send_approved_survey_email(survey_id):
    """Send the raw-data email with only the signed survey-form PDF attached."""
    survey = (
        Survey.query.filter_by(id=survey_id)
        .with_for_update()
        .first()
    )
    if survey is None:
        db.session.rollback()
        return "not_found"

    if survey.raw_video_email_sent_at:
        db.session.rollback()
        return "already_sent"

    if not survey.survey_dates_approved or not survey.survey_form_approved:
        db.session.rollback()
        return "waiting_for_approval"

    if not (
        survey.extracted_survey_start_date
        and survey.extracted_survey_end_date
        and survey.end_survey_pdf
    ):
        db.session.rollback()
        return "missing_survey_data"

    try:
        start_date = survey.extracted_survey_start_date.isoformat()
        end_date = survey.extracted_survey_end_date.isoformat()
        selected_week = week_of(survey.start_time) or current_week_number()
        subject = build_subject(survey, "raw", end_date)
        html_body = build_email_body(
            survey,
            "raw",
            start_date,
            end_date,
            selected_week,
        )
        attachment_bytes = download_file_from_drive(survey.end_survey_pdf)
        attachment_filename = (
            f"{survey.upc_code or survey.section_no}_Cycle-"
            f"{survey.cycle_no}_Survey_Form.pdf"
        )
        message_id = send_gmail_email(
            to_email=TO_EMAIL,
            from_email=FROM_EMAIL,
            subject=subject,
            html_body=html_body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            message_id=f"<viama-survey-{survey.id}@viama.local>",
        )
        survey.raw_video_email_sent_at = datetime.now(
            timezone.utc
        ).replace(tzinfo=None)
        survey.raw_video_email_message_id = message_id
        db.session.commit()
        return "sent"
    except Exception:
        db.session.rollback()
        raise