"""
Tolerant parsing of user-supplied filter values.

Every dashboard reads its filters straight off the query string and casts them
inline - ``int(cycle)``, ``int(week)``, ``datetime.strptime(from_date, ...)``.
Those casts raise on anything that is not exactly what they expect, and the
exception escapes the view, so a single stray character in a URL returned a
hard 500 instead of a dashboard:

    /admin?cycle=abc          ValueError: invalid literal for int()
    /admin?cycle=%20          ValueError  (a blank-but-present field)
    /teamleader?survey_date=02/08/2026
                              ValueError: time data does not match format
    /roadvision?week=99999999999
                              OverflowError from timedelta(days=...)

None of that needs a stack trace. A filter value the portal cannot make sense
of is a filter that was not applied, so these helpers return ``None`` and the
caller's ``if value:`` guard skips the clause - the user gets the unfiltered
dashboard rather than an error page.

Deliberately *not* used for path parameters. ``<int:survey_id>`` is already
validated by the URL map, and a bad id there should stay a 404.
"""

import logging
from datetime import datetime

log = logging.getLogger(__name__)

#: Bounds the project-week filter. Week numbers feed ``timedelta(days=...)``,
#: which raises OverflowError well before int does, so the cast alone is not
#: enough - see ``?week=999999999999``. 10 000 weeks is ~192 years.
MAX_WEEK = 10000

DATE_FORMAT = "%Y-%m-%d"


def safe_int(raw, minimum=None, maximum=None):
    """``int(raw)`` that returns None instead of raising.

    Also returns None when the value is out of [minimum, maximum], so callers
    do not have to range-check separately.
    """

    if raw is None:
        return None

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.debug("ignoring unparseable integer filter %r", raw)
        return None

    if minimum is not None and value < minimum:
        return None

    if maximum is not None and value > maximum:
        return None

    return value


def safe_week(raw):
    """Project-week number, or None. Bounded so timedelta cannot overflow."""

    return safe_int(raw, minimum=1, maximum=MAX_WEEK)


def safe_date(raw, fmt=DATE_FORMAT):
    """``datetime.strptime(raw, fmt)`` that returns None instead of raising."""

    if raw is None:
        return None

    text = str(raw).strip()

    if not text:
        return None

    try:
        return datetime.strptime(text, fmt)
    except (TypeError, ValueError):
        log.debug("ignoring unparseable date filter %r", raw)
        return None


def safe_count(raw, default=0):
    """A non-negative defect count from a form field.

    ``int(request.form.get("ir_lhs_count", 0))`` 500s on an empty string, which
    is exactly what a browser posts for a cleared number input, and it happily
    stores a negative count when the client-side ``min="0"`` is bypassed.
    """

    value = safe_int(raw, minimum=0)

    return default if value is None else value
