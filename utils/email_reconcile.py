"""Reconcile hand-sent raw/defect mails with the portal's delivery flags.

The portal marks a mail as delivered only when its own Microsoft Graph send
succeeds (``raw_video_email_sent_at`` / ``defect_report_email_sent_at``). A
mail sent by hand from the Gmail account is invisible to that path, so the
delayed-report screen keeps offering it as "Ready to send" - and the
auto-sender would send it a second time.

This module looks in Gmail's Sent folder for the two generated subjects
("Submission of Raw Data" / "Submission of Survey Report"), matches the
survey by NH/UPC + section + cycle, and stamps the same flags the auto-sender
would have written. It is read-only against Gmail and never raises into a
request: a Gmail failure simply leaves the row for the next pass or for the
manual "Mark sent" button.

Only rows that are actually waiting to be mailed are checked, so a successful
stamp removes the row from the candidate set and no Gmail call is repeated.
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import and_, or_

from extensions import db
from models.db_models import Survey
from utils.defect_report_delay import (
    build_defect_email_index,
    build_raw_email_index,
    find_defect_report_email,
    find_raw_email,
    _is_retryable_gmail_error,
)

log = logging.getLogger(__name__)

#: Written to the *_email_message_id columns for a mail sent by hand.
MANUAL_MESSAGE_ID = "manual"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mark_raw_email_sent(survey, when=None, message_id=MANUAL_MESSAGE_ID):
    """Flag the raw-video mail as delivered without sending it.

    Returns True when the row changed, False when it was already marked.
    """
    if survey.raw_video_email_sent_at:
        return False
    survey.raw_video_email_sent_at = when or _utcnow()
    survey.raw_video_email_message_id = message_id
    db.session.commit()
    return True


def mark_defect_email_sent(survey, when=None, message_id=MANUAL_MESSAGE_ID):
    """Flag the defect-report mail as delivered without sending it."""
    if survey.defect_report_email_sent_at:
        return False
    survey.defect_report_email_sent_at = when or _utcnow()
    survey.defect_report_email_message_id = message_id
    db.session.commit()
    return True


def _candidate_query():
    """Rows the auto-sender could still mail, i.e. the ones worth checking.

    Mirrors ``pending_raw_email_candidates`` and the defect-report candidate
    filter, so every Gmail search is spent on a row that is genuinely waiting.
    """
    raw_pending = Survey.raw_video_email_sent_at.is_(None)
    defect_pending = and_(
        Survey.raw_video_email_sent_at.is_not(None),
        Survey.defect_report_email_sent_at.is_(None),
    )
    return (
        Survey.query.filter(
            Survey.survey_dates_approved.is_(True),
            Survey.survey_form_approved.is_(True),
            Survey.extracted_survey_start_date.is_not(None),
            Survey.extracted_survey_end_date.is_not(None),
            Survey.end_survey_pdf.is_not(None),
            or_(raw_pending, defect_pending),
        )
    )


def reconcile_candidates(limit=10, after_id=0):
    """The next batch of waiting rows, oldest first."""
    return (
        _candidate_query()
        .filter(Survey.id > after_id)
        .order_by(Survey.id.asc())
        .limit(max(1, int(limit)))
        .all()
    )


def _remaining_candidates(after_id=0):
    return _candidate_query().filter(Survey.id > after_id).count()


def reconcile_survey(survey, gmail, fast=False):
    """Stamp whichever of the two mails this survey was sent by hand.

    Checks the raw mail first; if that one is (now) marked, the defect mail is
    checked too, so a survey sent by hand end-to-end is settled in one pass.
    Returns the list of mail types that were stamped ("raw"/"defect").

    ``fast`` skips Gmail's quota-window retry so a request-path caller (the
    pre-send duplicate check) never waits a minute for a quota reset.
    """
    marked = []
    gmail_kwargs = {"attempts": 1, "quota_max_wait": 0} if fast else {}

    if not survey.raw_video_email_sent_at:
        index = build_raw_email_index(gmail, survey, **gmail_kwargs)
        match = find_raw_email(survey, index, gmail, **gmail_kwargs)
        if match and mark_raw_email_sent(
            survey,
            match["sent_at"],
            f"gmail:{match['message_id']}",
        ):
            marked.append("raw")
            log.info(
                "Survey %s: raw mail found in Gmail Sent (%s); marked sent",
                survey.id,
                match["sent_at"],
            )

    if survey.raw_video_email_sent_at and not survey.defect_report_email_sent_at:
        index = build_defect_email_index(gmail, survey, **gmail_kwargs)
        match = find_defect_report_email(survey, index, gmail, **gmail_kwargs)
        if match and mark_defect_email_sent(
            survey,
            match["sent_at"],
            f"gmail:{match['message_id']}",
        ):
            marked.append("defect")
            log.info(
                "Survey %s: defect mail found in Gmail Sent (%s); marked sent",
                survey.id,
                match["sent_at"],
            )

    return marked


def stamp_if_sent_by_hand(survey, email_type):
    """Return True when Gmail's Sent folder already has this survey's mail.

    Called by the auto-senders immediately before they would send. If a mail
    with the generated subject and this survey's identifiers is already in
    Sent, the delivery flag is stamped and the caller must skip sending.

    Deliberately best-effort: any Gmail problem returns False, so the caller
    sends exactly as it did before this guard existed. It never raises.
    """
    if email_type not in ("raw", "defect"):
        return False

    if email_type == "raw" and survey.raw_video_email_sent_at:
        return True
    if email_type == "defect" and survey.defect_report_email_sent_at:
        return True

    try:
        from google_drive import get_gmail

        gmail = get_gmail()
        marked = reconcile_survey(survey, gmail, fast=True)
    except Exception as exc:
        log.warning(
            "pre-send Gmail check failed for survey %s (%s); sending anyway: %s",
            getattr(survey, "id", "?"),
            email_type,
            exc,
        )
        return False

    return email_type in marked


def reconcile_pending_manual_sends(
    app,
    limit=5,
    after_id=0,
    max_seconds=60,
):
    """Check one batch of waiting rows against Gmail's Sent folder.

    Returns ``{"checked", "marked_raw", "marked_defect", "remaining",
    "next_cursor"}``. ``next_cursor`` is the last id examined, so the caller
    can continue from there without re-checking the same rows in a burst.
    Never raises.
    """
    results = {
        "checked": 0,
        "marked_raw": 0,
        "marked_defect": 0,
        "remaining": 0,
        "next_cursor": after_id,
    }
    started_at = time.monotonic()

    with app.app_context():
        try:
            candidates = reconcile_candidates(limit=limit, after_id=after_id)
        except Exception:
            log.exception("manual-mail reconcile: could not read candidates")
            return results

        if not candidates:
            return results

        try:
            from google_drive import get_gmail

            gmail = get_gmail()
        except Exception as exc:
            log.warning("manual-mail reconcile skipped: Gmail unavailable: %s", exc)
            results["error"] = "gmail_unavailable"
            return results

        for survey in candidates:
            if time.monotonic() - started_at >= max_seconds:
                break

            results["checked"] += 1
            results["next_cursor"] = survey.id

            try:
                marked = reconcile_survey(survey, gmail)
            except Exception as exc:
                db.session.rollback()
                if _is_retryable_gmail_error(exc):
                    log.warning(
                        "manual-mail reconcile paused on a transient Gmail "
                        "error: %s",
                        exc,
                    )
                    results["error"] = "gmail_transient"
                    break
                log.exception(
                    "manual-mail reconcile failed for survey %s",
                    survey.id,
                )
                continue

            if "raw" in marked:
                results["marked_raw"] += 1
            if "defect" in marked:
                results["marked_defect"] += 1

        try:
            results["remaining"] = _remaining_candidates(results["next_cursor"])
        except Exception:
            log.exception("manual-mail reconcile: could not count remaining rows")

    return results
