"""
Run the bucket video-count check the moment a survey is marked completed.

The nightly cron (`/cron/video-count-sync`) is still what makes this *correct*.
At the instant a captain submits their counts the GCS upload is often still in
flight, so an immediate check frequently reports a shortfall that resolves
itself minutes later. This hook exists for latency, not for correctness: it puts
a remark on the survey straight away, and the cron's rolling 10-day window
re-checks and self-heals until it passes. Neither replaces the other.

Why a session listener rather than a line in routes/captain.py: a survey can
reach "completed" from the captain flow, from the JSON API, or from a shell.
Every one of those goes through db.session, so listening here catches all of
them and cannot be forgotten when a fourth path is added.

Three listeners, mirroring core/engine.py's change capture:

``before_flush``  detects the status transition. It has to happen here - once
                  the transaction commits the ORM attribute history is gone.

``after_flush``   resolves ids. A survey inserted straight into "completed" has
                  id = None at diff time; by now the database has assigned one.

``after_commit``  does the work, on a SEPARATE session. The originating session
                  is between transactions there and can emit no SQL; a separate
                  session also means a failure in here can never roll back the
                  captain's completion, and cannot recurse, because these
                  listeners are registered on the scoped session only.

Every failure is swallowed and logged. A survey that could not be checked is
left exactly as the cron would find it.

Set VIDEO_COUNT_ON_COMPLETE=0 to disable without a deploy.
"""

import logging
import os

from sqlalchemy import event
from sqlalchemy.orm.attributes import get_history

log = logging.getLogger("viama.video_count_hook")

_INSTALLED = False

#: Objects seen changing status, then the ids resolved from them.
_OBJECTS_KEY = "_viama_vc_objects"
_IDS_KEY = "_viama_vc_ids"

COMPLETED = "completed"


def hook_enabled():
    return os.getenv("VIDEO_COUNT_ON_COMPLETE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _on_before_flush(session, flush_context, instances):

    if not hook_enabled():
        return

    try:

        from models.db_models import Survey

        watched = session.info.setdefault(_OBJECTS_KEY, [])

        for obj in list(session.dirty) + list(session.new):

            if not isinstance(obj, Survey):
                continue

            history = get_history(obj, "status")

            if history.has_changes():
                target = history.added[0] if history.added else None
            elif obj in session.new:
                # Inserted directly as completed - no history, but still a
                # transition from the consumer's point of view.
                target = obj.status
            else:
                continue

            if target == COMPLETED:
                watched.append(obj)

    except Exception:
        log.exception("video-count hook failed during before_flush")


def _on_after_flush(session, flush_context):

    watched = session.info.get(_OBJECTS_KEY)

    if not watched:
        return

    ids = session.info.setdefault(_IDS_KEY, set())

    for obj in watched:
        try:
            identifier = getattr(obj, "id", None)
            if identifier is not None:
                ids.add(identifier)
        except Exception:
            continue

    session.info[_OBJECTS_KEY] = []


def _on_after_commit(session):

    session.info.pop(_OBJECTS_KEY, None)

    ids = session.info.pop(_IDS_KEY, None)

    if not ids or not hook_enabled():
        return

    try:

        from sqlalchemy.orm import Session

        from extensions import db
        from utils.video_count_sync import (
            INLINE_FETCH_TIMEOUT_SECONDS,
            check_surveys,
        )

        worker = Session(bind=db.engine)

        try:

            summary = check_surveys(
                sorted(ids),
                session=worker,
                timeout=INLINE_FETCH_TIMEOUT_SECONDS,
            )

            log.info(
                "video-count check on completion: %s -> %s",
                sorted(ids),
                summary,
            )

            # The remark was written on `worker`, and the change-capture
            # listeners are bound to the scoped session only - so without this
            # the consuming site would never learn the remark had changed.
            # record_bulk_change is best-effort and swallows its own errors.
            if summary.get("checked"):

                from core.engine import record_bulk_change

                record_bulk_change(
                    "survey",
                    ids=sorted(ids),
                    reason="video-count check on completion",
                )

        finally:
            worker.close()

    except Exception:
        # Never let this surface. The survey is committed and completed; the
        # only thing lost is an immediate remark, which the cron will write.
        log.exception(
            "video-count check on completion failed for survey(s) %s",
            sorted(ids),
        )


def _on_rollback(session):
    session.info.pop(_OBJECTS_KEY, None)
    session.info.pop(_IDS_KEY, None)


def install_video_count_hook(app=None):
    """Attach the listeners. Idempotent.

    Installed from app.py rather than from core.api.register_api, so that
    turning the webhook change-capture off does not silently take the remark
    automation with it.
    """

    global _INSTALLED

    if _INSTALLED:
        return

    from extensions import db

    event.listen(db.session, "before_flush", _on_before_flush)
    event.listen(db.session, "after_flush", _on_after_flush)
    event.listen(db.session, "after_commit", _on_after_commit)
    event.listen(db.session, "after_rollback", _on_rollback)

    _INSTALLED = True

    log.info(
        "viama: video-count-on-complete hook installed (enabled=%s)",
        hook_enabled(),
    )
