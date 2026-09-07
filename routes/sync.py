import hmac
import os

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
