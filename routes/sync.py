import hmac
import os
import time

from flask import Blueprint
from flask import jsonify
from flask import request
from flask import session

from utils.video_count_sync import DEFAULT_WINDOW_DAYS
from utils.video_count_sync import sync_recent

sync_bp = Blueprint(
    "sync",
    __name__
)

#: Total wall-clock budget for one /cron/approved-survey-emails invocation.
#: Vercel terminates a function that overruns its max duration (300s on Hobby)
#: and never retries a failed cron invocation, so the budget has to sit below
#: the limit with room for the last email in flight.
CRON_EMAIL_MAX_SECONDS = 240

#: Emails sent per phase in one invocation. Both phases run in the same
#: invocation, so this is the per-day throughput of the whole route.
CRON_EMAIL_LIMIT = 10


@sync_bp.route("/cron/approved-survey-emails")
def approved_survey_email_sync():
    """Retry approved survey emails whose matching raw XLSX has arrived."""
    if not _authorised():
        return jsonify({"error": "forbidden"}), 403

    from utils.approved_survey_email import process_pending_approved_survey_emails
    from utils.approved_survey_defect_email import process_pending_approved_defect_emails

    deadline = time.monotonic() + CRON_EMAIL_MAX_SECONDS

    raw_result = process_pending_approved_survey_emails(
        limit=CRON_EMAIL_LIMIT,
        max_seconds=max(1, int(deadline - time.monotonic())),
    )
    defect_result = process_pending_approved_defect_emails(
        limit=CRON_EMAIL_LIMIT,
        max_seconds=max(1, int(deadline - time.monotonic())),
    )
    summary = {"raw_video_emails": raw_result, "defect_report_emails": defect_result}
    status = (
        503
        if raw_result.get("failed") or defect_result.get("failed")
        else 200
    )
    return jsonify(summary), status


def _authorised():
    """Allow either the cron secret or a logged-in admin."""

    secret = os.getenv("CRON_SECRET")

    supplied = request.headers.get("X-Cron-Secret")

    if not supplied:

        auth = request.headers.get("Authorization", "")

        if auth.startswith("Bearer "):
            supplied = auth[7:]

    if not supplied:
        supplied = request.args.get("secret")

    if secret and supplied and hmac.compare_digest(secret, supplied):
        return True

    return session.get("role") == "admin"


@sync_bp.route("/cron/video-count-sync")
def video_count_sync():
    """Compare enlisted video counts against the GCP bucket and write remarks.

    Driven by Vercel Cron. Also callable by a logged-in admin for an on-demand
    re-check, since bucket counts change as uploads land.
    """

    if not _authorised():
        return jsonify({"error": "forbidden"}), 403

    try:
        days = int(request.args.get("days", DEFAULT_WINDOW_DAYS))
    except ValueError:
        days = DEFAULT_WINDOW_DAYS

    dry_run = request.args.get("dry_run") == "1"

    summary = sync_recent(
        days=days,
        dry_run=dry_run
    )

    status = 503 if summary.get("error") else 200

    return jsonify(summary), status
