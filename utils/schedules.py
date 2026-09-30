"""
Shared team / day / state filtering, live cycle numbers and the "Extract"
clipboard payload for the schedules pages.

The three schedules views (admin.py, regional.py, teamleader.py) each used to
build their own query and their own near-identical template.  The filters, the
cycle derivation and the extract payload are the same apart from the state
scoping each view imposes, so they all live here.

Two things worth knowing about the cycle number:

* ``SurveyAssignment.cycle_no`` is a stale, mostly-``1`` column.  It is NOT what
  will be surveyed - see routes/admin.py:1677-1694.  The real number comes from
  the stretch's ``Survey`` history via :func:`live_cycles`.
* ``team`` is a key from ``core.config.TEAMS``, resolved to its states via
  ``states_for_team`` rather than by a second hardcoded list.
"""

from sqlalchemy import or_

from core.config import (
    SCHEDULE_DAYS,
    TEAM_KEYS,
    current_week_number,
    day_order_case,
    states_for_team,
    team_display,
    team_for_state,
    week_window,
)
from models.db_models import Survey, SurveyAssignment


#: A survey in one of these states never actually happened, so its cycle number
#: is still free and the retry reuses it.  Same list the captain's own cycle
#: derivation uses (routes/captain.py:303-316).
NOT_HAPPENED_STATUSES = ("cancelled", "rescheduled", "missed")

#: The captain-side equivalent - a rejected resurvey also frees the number.
NOT_HAPPENED_CAPTAIN_STATUSES = ("cancelled", "rescheduled")


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------


def team_options():
    """
    ``(value, label)`` pairs for the team dropdown, straight from TEAMS.

    The templates used to hardcode their own option list, which is how the
    "Godbole" team ended up labelled three different ways across the app.
    """
    return [(key, team_display(key)) for key in TEAM_KEYS]


def day_options():
    """``(value, label)`` pairs for the day dropdown - Mon-Fri only."""
    return [(day, day) for day in SCHEDULE_DAYS]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def read_filters(args):
    """Pull the three dropdown values off a request's query string.

    Unknown day / team values are dropped rather than passed to the query, so a
    hand-edited URL cannot widen the result set.
    """
    day = args.get("day")
    team = args.get("team")
    state = args.get("state")

    if day not in SCHEDULE_DAYS:
        day = None

    if team not in TEAM_KEYS:
        team = None

    return day, team, state


def apply_filters(query, day=None, team=None, state=None):
    """
    Narrow a ``SurveyAssignment`` query by day, team and state.

    ``state`` is applied last so it wins when it contradicts the team's state
    list - the narrower filter should be the one that applies.
    """
    if day:
        query = query.filter_by(survey_day=day)

    if team:
        query = query.filter(
            SurveyAssignment.state.in_(states_for_team(team))
        )

    if state:
        query = query.filter_by(state=state)

    return query


def ordered(query):
    """Weekday first, then state, then section - the order the tables display in."""
    return query.order_by(
        day_order_case(),
        SurveyAssignment.state,
        SurveyAssignment.section_no,
    ).all()


# ---------------------------------------------------------------------------
# Live cycle number
# ---------------------------------------------------------------------------


def current_week_start():
    """
    Naive start-of-week used to decide which surveys have already consumed a cycle.

    Built from the app's own week arithmetic (``current_week_number`` +
    ``week_window``) rather than a fresh IST/UTC calculation, so it lines up with
    the week numbering shown on the dashboards and used by the reports export.
    """
    return week_window(current_week_number())[0]


def _stretch_key(row):
    """A survey and an assignment describe the same stretch when all three match."""
    return (row.section_no, row.stretch_code, row.upc_code)


def live_cycles(schedules):
    """
    ``{assignment.id: cycle}`` - the cycle that will actually be surveyed.

    Mirrors the captain's derivation (routes/captain.py:251-328): the most recent
    survey for the same stretch decides the number, and one that never happened
    (cancelled / rescheduled / missed) keeps its number so the retry reuses it.
    A stretch with no history is cycle 1.

    Only surveys that started *before* this week are considered.  A section
    already surveyed earlier this week has consumed its cycle, so counting it
    would report next week's number instead of the one happening now.
    """
    if not schedules:
        return {}

    section_nos = {a.section_no for a in schedules if a.section_no}

    if not section_nos:
        return {a.id: 1 for a in schedules}

    week_start = current_week_start()

    history = (
        Survey.query.filter(
            Survey.section_no.in_(section_nos),
            or_(
                Survey.start_time < week_start,
                Survey.start_time.is_(None),
            )
        )
        .order_by(
            Survey.start_time.desc().nullslast(),
            Survey.id.desc()
        )
        .all()
    )

    # The ordering above puts each stretch's most recent survey first, so the
    # first row seen for a key is the one the captain would land on.
    latest = {}

    for survey in history:
        latest.setdefault(_stretch_key(survey), survey)

    cycles = {}

    for assignment in schedules:

        previous = latest.get(_stretch_key(assignment))

        if previous is None or previous.cycle_no is None:

            cycles[assignment.id] = 1

        elif (
            previous.status in NOT_HAPPENED_STATUSES
            or previous.captain_status in NOT_HAPPENED_CAPTAIN_STATUSES
        ):

            cycles[assignment.id] = previous.cycle_no

        else:

            cycles[assignment.id] = previous.cycle_no + 1

    return cycles


def team_for_schedule(schedule):
    """Team key owning the assignment's state, or None if the state is unmapped."""
    return team_for_state(schedule.state)


def section_cycle_no(schedule, cycle):
    """The combined ``Section_Cycle No`` label, as used in email subjects."""
    return "{}_Cycle{}".format(schedule.section_no, cycle)


# ---------------------------------------------------------------------------
# Extract payload
# ---------------------------------------------------------------------------


def extract_rows(schedules, cycles):
    """``[{"section_no": ..., "cycle_no": ...}, ...]`` in the displayed order."""
    return [
        {
            "section_no": schedule.section_no,
            "cycle_no": cycles.get(schedule.id, 1),
        }
        for schedule in schedules
    ]


def extract_text(rows):
    """
    Tab-separated, one assignment per line.

    Tabs rather than commas so the text lands in Excel / Sheets as two columns
    instead of one, and so a section number containing a comma cannot shift the
    cycle into the wrong field.
    """
    return "\n".join(
        "{}\t{}".format(row["section_no"], row["cycle_no"])
        for row in rows
    )
