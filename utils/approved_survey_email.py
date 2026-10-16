"""Send the approved survey-form email once both approvals are recorded."""

from datetime import datetime, timezone

from core.config import current_week_number, week_of
from extensions import db
from google_drive import download_file_from_drive
from models.db_models import Survey
from utils.email_templates import build_email_body, build_subject
from utils.microsoft_graph_mail import send_microsoft_graph_email
from utils.raw_xlsx import download_raw_xlsx


TO_EMAIL = "mail.hunterrr@gmail.com"
FROM_EMAIL = "projects9@adorpower.com"


def send_approved_survey_email(survey_id):
    """Send the approved survey email with its PDF and raw XLSX attachments."""
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
        raw_xlsx_bytes, raw_xlsx_filename = download_raw_xlsx(
            survey.section_no,
            survey.cycle_no,
            survey.survey_type,
        )
        message_id = send_microsoft_graph_email(
            to_email=TO_EMAIL,
            from_email=FROM_EMAIL,
            subject=subject,
            html_body=html_body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            additional_attachments=[(raw_xlsx_bytes, raw_xlsx_filename)],
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