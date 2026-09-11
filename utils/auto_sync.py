import logging
from datetime import datetime

from extensions import db
from models.db_models import Survey
from google_drive import get_gmail
from gemini_utils import extract_survey_dates_from_drive
from utils.defect_report_delay import (
    build_defect_email_index,
    find_defect_report_email,
    defect_report_delay_days,
)

log = logging.getLogger(__name__)


def sync_defect_delay_for_survey(app, survey_id):
    """
    Background job to sync defect-report delay for one survey.

    The survey must have:
        - survey_form_completed = True
        - task1_completed = True
        - task2_completed = True
        - end_survey_pdf present

    The job:
        1. Extracts and saves the survey end date from the PDF.
        2. Checks Gmail for the matching defect-report email.
        3. Saves the Gmail sent date if found.
        4. Calculates delay days.
    """

    with app.app_context():
        survey = None

        try:
            survey = Survey.query.get(survey_id)

            if not survey:
                log.warning(
                    "Automatic defect delay sync: survey %s not found",
                    survey_id,
                )
                return

            # --------------------------------------------------
            # DOUBLE-CHECK ELIGIBILITY
            # --------------------------------------------------
            if not (
                survey.survey_form_completed
                and survey.task1_completed
                and survey.task2_completed
                and survey.end_survey_pdf
            ):
                log.info(
                    "Survey %s is not ready for defect delay sync",
                    survey_id,
                )
                return

            # --------------------------------------------------
            # DO NOT REPROCESS ALREADY MATCHED SURVEYS
            #
            # IMPORTANT:
            # "not_found" and "error" are intentionally allowed
            # to run again because the Gmail email may arrive later
            # or a temporary API error may have occurred.
            # --------------------------------------------------
            if survey.defect_report_match_status == "matched":
                log.info(
                    "Survey %s already matched; skipping",
                    survey_id,
                )
                return

            # --------------------------------------------------
            # MARK AS PENDING
            # --------------------------------------------------
            survey.defect_report_match_status = "pending"
            db.session.commit()

            # --------------------------------------------------
            # STEP 1:
            # EXTRACT SURVEY END DATE FROM DRIVE/GEMINI
            # --------------------------------------------------
            if not survey.extracted_survey_end_date:

                log.info(
                    "Survey %s: extracting survey end date from Drive/Gemini",
                    survey_id,
                )

                dates = extract_survey_dates_from_drive(
                    survey.end_survey_pdf
                )

                survey.extracted_survey_end_date = datetime.strptime(
                    dates["end_date"],
                    "%Y-%m-%d",
                ).date()

                survey.survey_end_date_confidence = (
                    dates["end_confidence"]
                )

                # IMPORTANT:
                # Save the Gemini result BEFORE touching Gmail.
                #
                # If Gmail fails afterwards, the extracted survey
                # end date will NOT be rolled back.
                db.session.commit()

                log.info(
                    "Survey %s: extracted end date=%s confidence=%s",
                    survey_id,
                    survey.extracted_survey_end_date,
                    survey.survey_end_date_confidence,
                )

            # --------------------------------------------------
            # STEP 2:
            # CONNECT TO GMAIL
            # --------------------------------------------------
            log.info(
                "Survey %s: connecting to Gmail",
                survey_id,
            )

            gmail = get_gmail()

            # --------------------------------------------------
            # STEP 3:
            # INDEX SENT DEFECT REPORT EMAILS
            # --------------------------------------------------
            log.info(
                "Survey %s: indexing Gmail Sent messages",
                survey_id,
            )

            email_index = build_defect_email_index(gmail)

            log.info(
                "Survey %s: indexed %s defect-report emails",
                survey_id,
                len(email_index),
            )

            # --------------------------------------------------
            # STEP 4:
            # FIND MATCHING DEFECT REPORT EMAIL
            # --------------------------------------------------
            match = find_defect_report_email(
                survey,
                email_index,
                gmail,
            )

            # --------------------------------------------------
            # NO MATCH
            # --------------------------------------------------
            if not match:

                survey.defect_report_match_status = "not_found"
                survey.defect_report_sent_at = None
                survey.defect_report_sent_confidence = None
                survey.defect_report_email_id = None
                survey.defect_report_delay_days = None

                db.session.commit()

                log.info(
                    "Survey %s: no matching defect-report email found. "
                    "It can be retried later.",
                    survey_id,
                )

                return

            # --------------------------------------------------
            # MATCH FOUND
            # --------------------------------------------------
            survey.defect_report_sent_at = match["sent_at"]
            survey.defect_report_sent_confidence = 1.0
            survey.defect_report_email_id = match["message_id"]

            # --------------------------------------------------
            # STEP 5:
            # CALCULATE DELAY
            # --------------------------------------------------
            survey.defect_report_delay_days = defect_report_delay_days(
                survey.extracted_survey_end_date,
                match["sent_at"].date(),
            )

            survey.defect_report_match_status = "matched"

            db.session.commit()

            log.info(
                "Automatic defect delay sync completed: "
                "survey=%s end_date=%s sent_at=%s delay_days=%s",
                survey_id,
                survey.extracted_survey_end_date,
                survey.defect_report_sent_at,
                survey.defect_report_delay_days,
            )

        except Exception as exc:

            # --------------------------------------------------
            # ERROR HANDLING
            # --------------------------------------------------
            db.session.rollback()

            try:
                survey = Survey.query.get(survey_id)

                if survey:
                    survey.defect_report_match_status = "error"
                    db.session.commit()

            except Exception:
                db.session.rollback()

            log.exception(
                "Automatic defect delay sync failed for survey %s: %s",
                survey_id,
                exc,
            )


def start_defect_delay_sync_if_ready(survey):
    """
    Start the automatic defect delay sync when all required
    conditions are satisfied.

    IMPORTANT:
    Surveys with "not_found" or "error" are allowed to be
    retried. Only "matched" surveys are skipped.
    """

    if not (
        survey.survey_form_completed
        and survey.task1_completed
        and survey.task2_completed
        and survey.end_survey_pdf
    ):
        return

    # Already successfully matched.
    if survey.defect_report_match_status == "matched":
        return

    from flask import current_app

    app = current_app._get_current_object()

    thread = Thread(
        target=sync_defect_delay_for_survey,
        args=(app, survey.id),
        daemon=True,
    )

    thread.start()

    log.info(
        "Started automatic defect delay sync for survey %s "
        "(current status=%s)",
        survey.id,
        survey.defect_report_match_status,
    )