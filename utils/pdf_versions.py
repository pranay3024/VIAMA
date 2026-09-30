"""
Keep every survey-form PDF the captain has ever uploaded, not just the latest.

``Survey.end_survey_pdf`` is a single column that gets overwritten on every
re-upload, and routes/captain.py used to delete the previous file from Drive as
well - so once a form was corrected, nothing an approver had seen before was
recoverable. Each upload now appends a row to ``survey_pdf_versions`` and the
older Drive files are left alone; the survey details page
(templates/admin/survey_details.html) lists them oldest first, each with the time
it was uploaded.

Reading is tolerant on purpose. ``ensure_history`` backfills a legacy survey that
has a PDF but no history rows, and every function here fails open: if the table
has not been migrated yet, the portal still shows the single latest PDF rather
than a 500. Run ``python migrate_survey_pdf_versions.py`` to create and backfill
the table.
"""

import logging

from sqlalchemy import func

log = logging.getLogger(__name__)


def _model():
    """Import lazily so a missing table/migration cannot break app startup."""

    from models.db_models import SurveyPdfVersion

    return SurveyPdfVersion


def next_version_no(survey_id):
    """The version number the next upload for this survey will get."""

    try:
        model = _model()

        highest = (
            model.query.with_entities(
                func.max(model.version_no)
            ).filter(
                model.survey_id == survey_id
            ).scalar()
        )

        return int(highest or 0) + 1

    except Exception:

        _rollback()

        log.warning(
            "pdf version numbering unavailable for survey %s; using 1",
            survey_id,
            exc_info=True,
        )

        return 1


def record_version(
    survey,
    pdf_url,
    uploaded_at=None,
    uploaded_by_role=None,
    uploaded_by_email=None,
    reupload_reason=None,
    version_no=None,
):
    """Append a row for ``pdf_url`` and mark it as the current PDF.

    Returns the new ``SurveyPdfVersion``, or ``None`` when the table is not
    available - callers must not depend on the return value for correctness.
    """

    url = (pdf_url or "").strip()

    if not url or not getattr(survey, "id", None):
        return None

    try:
        from datetime import datetime

        from extensions import db

        model = _model()

        stored_at = uploaded_at or datetime.utcnow()

        if version_no is None:
            version_no = next_version_no(survey.id)

        _clear_current(survey.id)

        version = model(
            survey_id=survey.id,
            pdf_url=url,
            uploaded_at=stored_at,
            version_no=version_no,
            uploaded_by_role=uploaded_by_role,
            uploaded_by_email=uploaded_by_email,
            is_current=True,
            reupload_reason=reupload_reason,
        )

        db.session.add(version)

        return version

    except Exception:

        _rollback()

        log.exception(
            "failed to record PDF version for survey %s",
            getattr(survey, "id", None),
        )

        return None


def ensure_history(survey):
    """
    Backfill a single version for a survey uploaded before this table existed.

    Only writes when there are no rows at all, so it is safe to call on every
    render. Returns the list of versions either way (possibly empty).
    """

    try:
        model = _model()

        existing = list_versions(survey.id)

        if existing:
            return existing

        url = (survey.end_survey_pdf or "").strip()

        if not url:
            return existing

        try:
            from extensions import db

            db.session.add(
                model(
                    survey_id=survey.id,
                    pdf_url=url,
                    uploaded_at=survey.survey_pdf_uploaded_at,
                    version_no=1,
                    uploaded_by_role="captain",
                    is_current=True,
                )
            )

            db.session.commit()

        except Exception:

            _rollback()

            log.warning(
                "pdf history backfill failed for survey %s",
                survey.id,
                exc_info=True,
            )

            return existing

        return list_versions(survey.id)

    except Exception:

        _rollback()

        log.warning(
            "pdf history unavailable for survey %s",
            getattr(survey, "id", None),
            exc_info=True,
        )

        return []


def list_versions(survey_id):
    """Every version for the survey, oldest first (original PDF on top)."""

    try:
        model = _model()

        rows = (
            model.query.filter(
                model.survey_id == survey_id
            ).order_by(
                model.version_no.asc(),
                model.id.asc(),
            ).all()
        )

        return list(rows)

    except Exception:

        _rollback()

        log.warning(
            "pdf versions unavailable for survey %s",
            survey_id,
            exc_info=True,
        )

        return []


def latest_version(survey_id):
    """The most recently uploaded version, or ``None``."""

    versions = list_versions(survey_id)

    if not versions:
        return None

    return versions[-1]


def _clear_current(survey_id):
    from extensions import db

    model = _model()

    db.session.query(model).filter(
        model.survey_id == survey_id,
        model.is_current.is_(True),
    ).update(
        {model.is_current: False},
        synchronize_session=False,
    )


def _rollback():
    try:
        from extensions import db

        db.session.rollback()

    except Exception:

        pass