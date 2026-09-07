"""
Hide soft-deleted records from portal queries.

The API soft-deletes by writing a tombstone to ``api_deleted_records`` rather
than flagging the row (see core/models.py:DeletedRecord for why). It also flips
``show_on_dashboard`` / ``show_in_teamleader_dashboard``, which is what /admin,
/regional and /teamleader filter on - but the roadvision and captain screens
never looked at those flags, so a deleted survey stayed visible there.

Filtering on the tombstone rather than on the dashboard flags is deliberate:
an admin can toggle ``show_on_dashboard`` on its own to declutter a dashboard,
and that is not the same thing as deleting the record. Only the tombstone means
deleted.

Fails open. If the API tables do not exist yet, or core/ is unavailable, the
query comes back untouched - the portal showing one row too many is a far better
outcome than the portal 500ing.
"""

import logging

log = logging.getLogger(__name__)


def exclude_deleted(query, model):
    """Return `query` with soft-deleted rows of `model` filtered out."""

    try:
        from core.engine import entity_of
        from core.models import DeletedRecord

        tombstones = DeletedRecord.query.with_entities(
            DeletedRecord.entity_id
        ).filter(
            DeletedRecord.entity == entity_of(model)
        )

        return query.filter(~model.id.in_(tombstones))

    except Exception:

        try:
            from extensions import db

            db.session.rollback()
        except Exception:
            pass

        log.debug("soft-delete filter unavailable; returning query unfiltered",
                  exc_info=True)

        return query
