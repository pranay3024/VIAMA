import logging
from datetime import datetime
from threading import Thread

from extensions import db
from models.db_models import Survey
from google_drive import get_gmail
from gemini_utils import extract_survey_dates_from_drive
from utils.defect_report_delay import (
    build_defect_email_index,
    find_defect_report_email,
    working_days_between,
)

log = logging.getLogger(__name__)

def sync_defect_delay_for_survey(app, survey_id):
    """
    Background job to sync the defect report delay when all tasks are completed.
    Runs inside the Flask application context.
    """
    with app.app_context():
        try:
            survey = Survey.query.get(survey_id)
            if not survey:
                return
            
            # Double check all conditions are met
            if not (survey.survey_form_completed and 
                    survey.task1_completed and 
                    survey.task2_completed and 
                    survey.end_survey_pdf):
                return
            
            # Avoid re-running if it was already processed recently by another thread
            if survey.defect_report_match_status not in [None, "pending"]:
                return
                
            survey.defect_report_match_status = "pending"
            db.session.commit()
            
            # Step 1: Extract dates using Gemini if not already done
            if not survey.extracted_survey_end_date:
                dates = extract_survey_dates_from_drive(survey.end_survey_pdf)
                survey.extracted_survey_end_date = datetime.strptime(
                    dates["end_date"], "%Y-%m-%d"
                ).date()
                survey.survey_end_date_confidence = dates["end_confidence"]
            
            # Step 2: Index Gmail and find the matching defect email
            gmail = get_gmail()
            email_index = build_defect_email_index(gmail)
            match = find_defect_report_email(survey, email_index, gmail)
            
            if not match:
                survey.defect_report_match_status = "not_found"
                survey.defect_report_sent_at = None
                survey.defect_report_delay_days = None
            else:
                survey.defect_report_sent_at = match["sent_at"]
                survey.defect_report_sent_confidence = 1.0
                survey.defect_report_email_id = match["message_id"]
                
                raw_delay_days = working_days_between(
                    survey.extracted_survey_end_date,
                    match["sent_at"].date(),
                )
                survey.defect_report_delay_days = max(raw_delay_days - 3, 0)
                survey.defect_report_match_status = "matched"
                
            db.session.commit()
            log.info(f"Automatic defect delay sync completed for survey {survey.id}")
            
        except Exception as exc:
            db.session.rollback()
            survey = Survey.query.get(survey_id)
            if survey:
                survey.defect_report_match_status = "error"
                db.session.commit()
            log.exception(f"Automatic defect delay sync failed for survey {survey_id}: {exc}")

def start_defect_delay_sync_if_ready(survey):
    """
    Checks if a survey is ready for defect delay calculation, and if so,
    starts a background thread to process it.
    """
    if (survey.survey_form_completed and 
        survey.task1_completed and 
        survey.task2_completed and 
        survey.end_survey_pdf and 
        survey.defect_report_match_status in [None, "error", "not_found"]):
        
        from flask import current_app
        app = current_app._get_current_object()
        
        # Start processing in a background thread to prevent blocking the UI
        thread = Thread(target=sync_defect_delay_for_survey, args=(app, survey.id))
        thread.daemon = True
        thread.start()