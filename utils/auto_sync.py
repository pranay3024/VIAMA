import logging
import threading

from extensions import db
from models.db_models import Survey
from google_drive import get_gmail
from gemini_utils import (
    extract_survey_form_fields_from_drive,
    extract_survey_form_fields_from_pdf,
    apply_survey_form_fields,
)
from utils.defect_report_delay import (
    build_defect_email_index,
    find_defect_report_email,
    defect_report_delay_days,
    _is_retryable_gmail_error,
)

log = logging.getLogger(__name__)

print("[DEBUG_FLOW] utils.auto_sync module loaded (extract_survey_form_fields active)", flush=True)

# Cap how many per-survey syncs may touch Google (Drive/Gemini/Gmail) at once.
# TL "YES" clicks arrive in bursts (50/day); the parallel bursts are what
# caused connection drops / API timeouts, so let at most a few run at a time.
_SYNC_SEMAPHORE = threading.BoundedSemaphore(3)

# Gemini is metered (per-request). A PDF that fails extraction is retried, but
# never forever - after this many failed auto attempts we stop paying for it and
# leave the survey for manual/admin correction. 2 attempts absorbs flaky
# network blips without burning credits on an unreadable PDF.
MAX_AUTO_EXTRACT_ATTEMPTS = 2


def extract_survey_form_fields(survey, pdf_bytes=None, view_url=None):
    """
    Extract ALL survey-form fields in a SINGLE Gemini call and persist them.

    Extracts the start date, end date, AE/IE/SC name, PIU name and
    Contractor/O&M agency together in one request. Called once at PDF upload
    time (with ``pdf_bytes`` in hand) and as a fallback for pre-existing
    surveys whose end date was never extracted (by ``view_url``).

    In the common case a form is extracted exactly once: once both dates are
    stored the guard below refuses further calls. The one allowed exception is
    a form where that first call captured the end date but returned a null
    start date - Gemini's single response may miss one handwritten value even
    though the rest are read fine. Rather than leaving the start date missing
    forever, such surveys are retried (still capped by
    MAX_AUTO_EXTRACT_ATTEMPTS, so at most one extra call per form). Stored
    values are never overwritten, and failures are logged and swallowed so the
    caller (a form upload) is never blocked. Returns True on success, False if
    skipped or failed.
    """
    attempts = getattr(survey, "end_date_extract_attempts", None) or 0
    print(
        f"[DEBUG_FLOW] extract_survey_form_fields survey={survey.id} "
        f"pdf_bytes={bool(pdf_bytes)} view_url={bool(view_url)} "
        f"end={survey.extracted_survey_end_date} start={survey.extracted_survey_start_date} "
        f"attempts={attempts}",
        flush=True,
    )

    if not (pdf_bytes or view_url):
        print(f"[DEBUG_FLOW] survey={survey.id} SKIP: no pdf bytes or view_url", flush=True)
        return False

    if survey.extracted_survey_end_date and survey.extracted_survey_start_date:
        print(f"[DEBUG_FLOW] survey={survey.id} SKIP: both dates already stored", flush=True)
        return False

    if attempts >= MAX_AUTO_EXTRACT_ATTEMPTS:
        print(
            f"[DEBUG_FLOW] survey={survey.id} SKIP: attempts {attempts} >= "
            f"MAX {MAX_AUTO_EXTRACT_ATTEMPTS}",
            flush=True,
        )
        return False

    survey.end_date_extract_attempts = attempts + 1
    db.session.commit()

    try:
        fields = (
            extract_survey_form_fields_from_pdf(pdf_bytes)
            if pdf_bytes is not None
            else extract_survey_form_fields_from_drive(view_url)
        )
        apply_survey_form_fields(survey, fields)
        db.session.commit()
        print(
            f"[DEBUG_FLOW] survey={survey.id} EXTRACT OK "
            f"start={survey.extracted_survey_start_date} "
            f"end={survey.extracted_survey_end_date} "
            f"ae={survey.extracted_ae_ie_sc_name!r} "
            f"piu={survey.extracted_piu_name!r} "
            f"contractor={survey.extracted_contractor_agency!r}",
            flush=True,
        )
        log.info(
            "Survey %s: form fields extracted start=%s end=%s ae=%s piu=%s "
            "contractor=%s",
            survey.id,
            survey.extracted_survey_start_date,
            survey.extracted_survey_end_date,
            survey.extracted_ae_ie_sc_name,
            survey.extracted_piu_name,
            survey.extracted_contractor_agency,
        )
        return True
    except Exception as exc:
        db.session.rollback()
        print(
            f"[DEBUG_FLOW] survey={survey.id} EXTRACT FAILED attempt={attempts + 1}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        log.exception(
            "Survey %s: form-field extraction failed (attempt %d): %s",
            survey.id,
            attempts + 1,
            exc,
        )
        return False


def extract_survey_end_date_if_missing(survey):
    """
    Best-effort extraction of ALL survey form fields via Gemini, on demand.

    Backwards-compatible wrapper used by admin paths (gmail-draft prefill,
    bulk sync). Extraction now returns every form field in one call; only the
    end date is consumed by callers that only need a date. Runs only from
    credit-bounded paths. An already-extracted date is never overwritten.
    """
    return extract_survey_form_fields(survey, view_url=survey.end_survey_pdf)


def sync_defect_delay_for_survey(app, survey_id):
    with _SYNC_SEMAPHORE:
        return _run_sync_defect_delay(app, survey_id)


def _run_sync_defect_delay(app, survey_id):
    """
    Background job to sync defect-report delay for one survey.

    The survey must have:
        - survey_form_completed = True
        - task1_completed = True
        - task2_completed = True
        - end_survey_pdf present

    The job:
        1. Extracts and saves all survey-form fields (dates + printed fields).
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

            print(
                f"[DEBUG_FLOW] sync_defect_delay survey={survey_id} "
                f"form_completed={survey.survey_form_completed} "
                f"task1={survey.task1_completed} task2={survey.task2_completed} "
                f"pdf={bool(survey.end_survey_pdf)} "
                f"start={survey.extracted_survey_start_date} "
                f"end={survey.extracted_survey_end_date} "
                f"attempts={getattr(survey, 'end_date_extract_attempts', 0) or 0} "
                f"match_status={survey.defect_report_match_status}",
                flush=True,
            )

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
            # The check is intentionally one-shot. A manual admin sync can
            # be used if a later retry is required.
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
            print(f"[DEBUG_FLOW] survey={survey_id} marked pending", flush=True)

            # --------------------------------------------------
            # STEP 1:
            # EXTRACT SURVEY FORM FIELDS FROM DRIVE/GEMINI
            #
            # One Gemini call extracts start + end date and the printed
            # fields (AE/IE/SC, PIU, Contractor). New workflows extract these
            # at PDF-upload time; this path fires for older surveys whose form
            # was uploaded before extraction existed, and for forms where the
            # upload-time call captured the end date but returned a null
            # start date (the retry is bounded by MAX_AUTO_EXTRACT_ATTEMPTS).
            # --------------------------------------------------
            if not (
                survey.extracted_survey_end_date
                and survey.extracted_survey_start_date
            ):
                print(
                    f"[DEBUG_FLOW] survey={survey_id} STEP-1: dates incomplete, "
                    f"attempting extraction",
                    flush=True,
                )

                extract_attempts = (
                    getattr(survey, "end_date_extract_attempts", None) or 0
                )

                if extract_attempts >= MAX_AUTO_EXTRACT_ATTEMPTS:

                    if not survey.extracted_survey_end_date:

                        # The PDF already failed extraction the max allowed
                        # times - stop spending Gemini credits on it. Without an
                        # end date a delay cannot be computed, so do not loop
                        # this survey forever; mark it done so the queue stops
                        # retrying it.
                        log.warning(
                            "Survey %s: skipping Gemini extraction after %d "
                            "failed auto attempts",
                            survey_id,
                            extract_attempts,
                        )
                        survey.defect_report_match_status = "not_found"
                        db.session.commit()
                        return

                    # The end date is already stored; only the start date is
                    # missing and the bounded retries are exhausted. The delay
                    # can still be computed from the end date, so leave the
                    # survey for manual correction and continue to Gmail
                    # matching instead of aborting the sync.
                    log.warning(
                        "Survey %s: start date could not be extracted after "
                        "%d auto attempts; continuing with end date only",
                        survey_id,
                        extract_attempts,
                    )

                else:

                    log.info(
                        "Survey %s: extracting survey form fields from Drive/Gemini "
                        "(auto attempt %d)",
                        survey_id,
                        extract_attempts + 1,
                    )

                    survey.end_date_extract_attempts = extract_attempts + 1
                    db.session.commit()

                    fields = extract_survey_form_fields_from_drive(
                        survey.end_survey_pdf
                    )
                    apply_survey_form_fields(survey, fields)

                    # IMPORTANT:
                    # Save the Gemini result BEFORE touching Gmail.
                    #
                    # If Gmail fails afterwards, the extracted dates/fields will
                    # NOT be rolled back.
                    db.session.commit()

                    log.info(
                        "Survey %s: extracted start=%s end=%s confidence=%s",
                        survey_id,
                        survey.extracted_survey_start_date,
                        survey.extracted_survey_end_date,
                        survey.survey_end_date_confidence,
                    )

            # --------------------------------------------------
            # STEP 2:
            # CONNECT TO GMAIL
            # --------------------------------------------------
            print(f"[DEBUG_FLOW] survey={survey_id} STEP-2: connecting to Gmail", flush=True)
            log.info(
                "Survey %s: connecting to Gmail",
                survey_id,
            )

            gmail = get_gmail()

            # --------------------------------------------------
            # STEP 3:
            # INDEX SENT DEFECT REPORT EMAILS
            # --------------------------------------------------
            print(f"[DEBUG_FLOW] survey={survey_id} STEP-3: indexing Gmail sent", flush=True)
            log.info(
                "Survey %s: indexing Gmail Sent messages",
                survey_id,
            )

            # Narrow the search to this survey's identifiers (nh_number /
            # upc_code) so Gmail only returns matching mails instead of the
            # whole Sent mailbox - this is what keeps the sync near-instant.
            email_index = build_defect_email_index(gmail, survey)

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

            print(
                f"[DEBUG_FLOW] survey={survey_id} STEP-4: indexed={len(email_index)} "
                f"match={'YES' if match else 'NO'}",
                flush=True,
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
            if survey.extracted_survey_end_date:
                survey.defect_report_delay_days = defect_report_delay_days(
                    survey.extracted_survey_end_date,
                    match["sent_at"].date(),
                )
            else:
                # No end date yet (extraction failed / not retried enough):
                # record the match but leave the delay blank rather than
                # crashing the whole sync. A manual correction can supply it.
                survey.defect_report_delay_days = None
                print(
                    f"[DEBUG_FLOW] survey={survey_id} STEP-5: no extracted end "
                    f"date, delay left blank",
                    flush=True,
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

            # A transient Gmail failure (403/429 quota or rate-limit, 5xx,
            # dropped connection) must NOT poison this survey into ``error``:
            # the auto-sweep only ever picks up ``pending`` surveys, so an
            # ``error`` would sit dead until a manual admin retry. Requeue it
            # instead - the next sweep poll retries and normally completes the
            # moment Gmail's per-minute user quota window has reset. These
            # failures burn no Gemini credits (dates are already persisted
            # before Gmail runs) and re-runs are bounded by the extraction
            # attempt cap.
            transient = _is_retryable_gmail_error(exc)

            try:
                survey = Survey.query.get(survey_id)

                if survey:
                    survey.defect_report_match_status = (
                        "pending" if transient else "error"
                    )
                    db.session.commit()

            except Exception:
                db.session.rollback()

            log.exception(
                "Automatic defect delay sync failed for survey %s: %s",
                survey_id,
                exc,
            )
            print(
                f"[DEBUG_FLOW] survey={survey_id} SYNC FAILED: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


def process_pending_defect_delays(app, limit=6):
    """Process surveys in the queue inside this one request.

    Picks up ``pending`` surveys only. ``error`` surveys are deliberately left
    alone: auto-sweeps fire on every page load/poll, and re-running a survey
    whose date extraction failed would spend another Gemini call each time.
    Errors are retried explicitly (admin manual retry / CLI), never by the
    background sweep. Runs the selected surveys serially so Gemini and Gmail
    are never called in parallel. Doing the work on the request thread (not a
    fire-and-forget daemon thread) is what makes it reliable on serverless -
    the request stays alive until the selected chunk is done.
    """
    with app.app_context():
        queued = (
            Survey.query
            .filter(Survey.defect_report_match_status == "pending")
            .order_by(Survey.id.asc())
            .limit(limit)
            .all()
        )
        ids = [survey.id for survey in queued]
        if not ids:
            return 0, 0

    print(f"[DEBUG_FLOW] sweep found queued survey ids: {ids}", flush=True)

    log.info(
        "Defect delay sweep: processing %s queued surveys",
        len(ids),
    )

    processed = 0
    # Gemini and Gmail are external, quota-limited services. Process the
    # selected batch in one request so a 50-survey click never creates a
    # burst of concurrent provider calls.
    for survey_id in ids:
        try:
            sync_defect_delay_for_survey(app, survey_id)
            processed += 1
        except Exception as exc:
            log.exception("Defect delay sweep item failed: %s", exc)

    with app.app_context():
        remaining = (
            Survey.query
            .filter(Survey.defect_report_match_status == "pending")
            .count()
        )

    log.info(
        "Defect delay sweep done: processed=%s remaining=%s",
        processed,
        remaining,
    )
    return processed, remaining


def start_defect_delay_sync_if_ready(survey):
    """
    Start the automatic defect delay sync when all required
    conditions are satisfied.

    The check is one-shot. Any existing status means the survey was
    already checked and must not start another automatic run.
    """

    if not (
        survey.survey_form_completed
        and survey.task1_completed
        and survey.task2_completed
        and survey.end_survey_pdf
    ):
        print(
            f"[DEBUG_FLOW] start_defect_delay_sync survey={survey.id} "
            f"NOT_READY form={survey.survey_form_completed} "
            f"task1={survey.task1_completed} task2={survey.task2_completed} "
            f"pdf={bool(survey.end_survey_pdf)}",
            flush=True,
        )
        return

    # Queue the work and let the sweep process it outside the click request.
    # Gemini/Gmail are quota-limited and the team leader can complete up to 50
    # surveys in one batch.
    if survey.defect_report_match_status is not None:
        print(
            f"[DEBUG_FLOW] start_defect_delay_sync survey={survey.id} "
            f"ALREADY_PROCESSED status={survey.defect_report_match_status}",
            flush=True,
        )
        return

    log.info(
        "Running automatic defect delay sync for survey %s "
        "(current status=%s)",
        survey.id,
        survey.defect_report_match_status,
    )

    survey.defect_report_match_status = "pending"
    db.session.commit()
    print(
        f"[DEBUG_FLOW] start_defect_delay_sync survey={survey.id} QUEUED as pending",
        flush=True,
    )