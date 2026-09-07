"""
VIAMA - shared constants and derived-value logic.

Everything the portal hardcodes in several places (the project-week epoch, the
team->state map, status vocabularies, the two checklists) plus the values it
computes at render time but never stores (team KM totals, upload durations,
scheduled day, dashboard sort orders).

Imports nothing from models/routes, so it is safe to import anywhere.
"""

# ==========================================================================
# constants
# Single source of truth for the constants the portal currently hardcodes in
# several places.  Pure data + tiny helpers; no imports from models/routes/api.
#
# Mirrors:
#   PROJECT_START   -> routes/admin.py:146,192,796,974,1153
#                      routes/regional.py:102, routes/teamleader.py:91,
#                      routes/roadvision.py:85
#   TEAMS           -> routes/admin.py:270-284, 834-857, 1008-1031
#   EXPORT_COLUMNS  -> routes/admin.py:1054-1067
# ==========================================================================

from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Project timeline
# ---------------------------------------------------------------------------

#: Week 1 of the project starts here.  Hardcoded in 8 places across routes/.
PROJECT_START = datetime(2026, 6, 22)

IST_OFFSET = timedelta(hours=5, minutes=30)
IST_TZ_NAME = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# Survey status machine
# ---------------------------------------------------------------------------

SURVEY_ONGOING = "ongoing"
SURVEY_GROUNDWORK_COMPLETED = "groundwork_completed"
SURVEY_VIDEO_PENDING = "video_pending"
SURVEY_COMPLETED = "completed"
#: Not present in the legacy portal.  Introduced by the API as a soft abort so a
#: stuck survey can be cleared without deleting history.  Because every portal
#: predicate uses a positive ``status.in_([...])`` list, a cancelled survey
#: simply stops matching and no template needed changing.
SURVEY_CANCELLED = "cancelled"

#: Display / sort order used by the admin dashboard.
SURVEY_STATUSES = (
    SURVEY_ONGOING,
    SURVEY_GROUNDWORK_COMPLETED,
    SURVEY_VIDEO_PENDING,
    SURVEY_COMPLETED,
)

SURVEY_STATUS_LABELS = {
    SURVEY_ONGOING: "Ongoing",
    SURVEY_GROUNDWORK_COMPLETED: "Groundwork Completed",
    SURVEY_VIDEO_PENDING: "Video Pending",
    SURVEY_COMPLETED: "Completed",
    SURVEY_CANCELLED: "Cancelled",
}

#: "A survey exists for this section this week" - routes/admin.py:453-458,
#: routes/captain.py:196-201.
ACTIVE_SURVEY_STATUSES = SURVEY_STATUSES

#: "This captain already has a survey running" - routes/captain.py:48,88,328,644,944.
IN_PROGRESS_STATUSES = (SURVEY_ONGOING, SURVEY_GROUNDWORK_COMPLETED)

#: Forward-only transitions enforced by the API.  The portal has no validator at
#: all; these are derived from the actual flow in routes/captain.py.
ALLOWED_STATUS_TRANSITIONS = {
    SURVEY_ONGOING: {SURVEY_GROUNDWORK_COMPLETED, SURVEY_CANCELLED},
    SURVEY_GROUNDWORK_COMPLETED: {SURVEY_VIDEO_PENDING, SURVEY_CANCELLED},
    SURVEY_VIDEO_PENDING: {SURVEY_COMPLETED},
    SURVEY_COMPLETED: set(),
    SURVEY_CANCELLED: set(),
}

# ---------------------------------------------------------------------------
# Assignment status
# ---------------------------------------------------------------------------

ASSIGNMENT_STATUSES = (
    "assigned",
    "started",
    "missed",
    "completed",
    "backup_in_progress",
    "completed_by_backup",
)

# ---------------------------------------------------------------------------
# Roles  (routes/auth.py:55-71)
# ---------------------------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_CAPTAIN = "captain"
ROLE_BACKUP_CAPTAIN = "backup_captain"
ROLE_REGIONAL_MANAGER = "regional_manager"
ROLE_TEAM_LEADER = "team_leader"
ROLE_ROADVISION = "roadvision"

ROLES = (
    ROLE_ADMIN,
    ROLE_CAPTAIN,
    ROLE_BACKUP_CAPTAIN,
    ROLE_REGIONAL_MANAGER,
    ROLE_TEAM_LEADER,
    ROLE_ROADVISION,
)

#: Landing page per role - routes/auth.py:55-71.
ROLE_LANDING = {
    ROLE_ADMIN: "/admin",
    ROLE_CAPTAIN: "/captain-home",
    ROLE_REGIONAL_MANAGER: "/regional",
    ROLE_TEAM_LEADER: "/teamleader",
    ROLE_BACKUP_CAPTAIN: "/backup-home",
    ROLE_ROADVISION: "/roadvision",
}

# ---------------------------------------------------------------------------
# Teams -> states
# ---------------------------------------------------------------------------
# NOTE the display quirk: the team keyed "Godbole" is rendered under a different
# name in the UI.  templates/admin/dashboard.html:112 now says "Odisha Team"
# (it said "Viama Team" until the dashboard was relabelled); reports.html:146-150
# still says "Viama".  The key is what the code joins on - only the label moved.

TEAMS = {
    "Krish": {"display": "Krish", "states": ("WEST BENGAL", "ASSAM", "BIHAR")},
    "Godbole": {"display": "Odisha", "states": ("ODISHA",)},
    "Aspizo": {"display": "Aspizo", "states": ("UTTAR PRADESH", "JHARKHAND")},
}

TEAM_KEYS = tuple(TEAMS.keys())

STATE_TO_TEAM = {
    state: key for key, meta in TEAMS.items() for state in meta["states"]
}

#: Hardcoded in templates/admin/reports.html:171-181.  Kept for the /meta/states
#: fallback when the DB has no assignments yet.
KNOWN_STATES = (
    "ASSAM",
    "BIHAR",
    "JHARKHAND",
    "ODISHA",
    "UTTAR PRADESH",
    "WEST BENGAL",
)


def team_for_state(state):
    """Return the team key owning ``state``, or None."""
    if not state:
        return None
    return STATE_TO_TEAM.get(state.strip().upper())


def states_for_team(team_key):
    """Return the tuple of states owned by ``team_key``, or () if unknown."""
    meta = TEAMS.get(team_key)
    return meta["states"] if meta else ()


def team_display(team_key):
    """Human-facing label for a team key ('Godbole' -> 'Viama')."""
    meta = TEAMS.get(team_key)
    return meta["display"] if meta else team_key


# ---------------------------------------------------------------------------
# Days
# ---------------------------------------------------------------------------

#: Only Mon-Fri are scheduled / counted (routes/admin.py:688-695, 707-725).
SCHEDULE_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

#: Full week, offered by the captain's survey-day picker
#: (templates/captain/select_stretch.html).
ALL_DAYS = SCHEDULE_DAYS + ("Saturday", "Sunday")

#: 1-based ordering used by the schedules CASE; anything else sorts as 6.
WEEKDAY_ORDER = {day: i + 1 for i, day in enumerate(SCHEDULE_DAYS)}
WEEKDAY_ORDER_ELSE = 6

# ---------------------------------------------------------------------------
# Survey type
# ---------------------------------------------------------------------------

SURVEY_TYPES = ("Day", "Night")
#: /captain/resurvey/start builds these (routes/captain.py:912).
RESURVEY_TYPES = ("Day Re-Survey", "Night Re-Survey")
ALL_SURVEY_TYPES = SURVEY_TYPES + RESURVEY_TYPES

# ---------------------------------------------------------------------------
# Video defect counts
# ---------------------------------------------------------------------------

DEFECT_COUNT_FIELDS = (
    "ir_lhs_count",
    "mcw_lhs_count",
    "service_lhs_count",
    "slip_lhs_count",
    "ir_rhs_count",
    "mcw_rhs_count",
    "service_rhs_count",
    "slip_rhs_count",
)

#: field -> (side, human label)  - templates/captain/video_counts.html
DEFECT_FIELD_META = {
    "ir_lhs_count": ("LHS", "Intersection Road"),
    "mcw_lhs_count": ("LHS", "Main Carriageway"),
    "service_lhs_count": ("LHS", "Service Road"),
    "slip_lhs_count": ("LHS", "Slip Road"),
    "ir_rhs_count": ("RHS", "Intersection Road"),
    "mcw_rhs_count": ("RHS", "Main Carriageway"),
    "service_rhs_count": ("RHS", "Service Road"),
    "slip_rhs_count": ("RHS", "Slip Road"),
}

# ---------------------------------------------------------------------------
# Team-leader task labels
# ---------------------------------------------------------------------------
# The column names and the UI labels disagree; preserve both.

TASK_FIELDS = {
    "survey_form": ("survey_form_completed", "survey_form_completed_at", "Survey Form"),
    "task1": ("task1_completed", "task1_completed_at", "Raw Video"),
    "task2": ("task2_completed", "task2_completed_at", "Final Report"),
}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Upload duration turns red past this (all four dashboard templates).
UPLOAD_DURATION_RED_MINUTES = 480

#: Missed-survey engine cutoffs, in IST hours (routes/admin.py:425, 518).
ALERT_CUTOFF_HOUR_PRIMARY = 14
ALERT_CUTOFF_HOUR_SECONDARY = 16

# ---------------------------------------------------------------------------
# Reporting / export
# ---------------------------------------------------------------------------

#: routes/admin.py:1054-1067 - order matters, the export is byte-compatible.
EXPORT_COLUMNS = (
    "Date",
    "Cycle",
    "Captain",
    "State",
    "Section No",
    "UPC Code",
    "Stretch",
    "KM",
    "Survey Type",
    "Status",
)

#: routes/admin.py:1178-1184 - the statuses the Gmail-draft picker considers.
EMAIL_DRAFT_STATUSES = (SURVEY_ONGOING, SURVEY_VIDEO_PENDING, SURVEY_COMPLETED)

EMAIL_TYPES = ("defect", "raw")

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SUPABASE_PDF_BUCKET = "survey-pdfs"

#: Media columns on Survey that the API will let you set.
#: The last two are the RoadVision spreadsheet uploads added in
#: routes/roadvision.py (upload_defect_report / upload_raw_video); they hold a
#: Drive view_url exactly like the photo and PDF columns do.
MEDIA_FIELDS = (
    "dashcam_photo",
    "settings_photo",
    "end_survey_photo",
    "end_survey_pdf",
    "defect_report_file",
    "raw_video_excel_file",
)

#: Declared on the model but never written by any route.  Excluded from
#: serialization unless ?include_deprecated=true.
DEAD_SURVEY_COLUMNS = ("settings_photo",)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# ==========================================================================
# timeutils
# Timezone handling.
#
# The database is genuinely inconsistent, so this module classifies every
# datetime column and converts explicitly rather than guessing:
#
#   IST_WALL  the naive value stored in Postgres is IST wall-clock.
#             Written by ``datetime.now(pytz.timezone("Asia/Kolkata"))`` into a
#             naive ``DateTime`` column, so psycopg2 drops the +05:30 offset and
#             the local time is what lands on disk.
#             -> surveys.start_time, surveys.end_time
#                (routes/captain.py:271-273, 538-540)
#
#   UTC_WALL  the naive value stored is UTC wall-clock.
#             Written by ``datetime.utcnow()``.
#             -> everything else.
#
#   DATE_ONLY a plain date, no timezone meaning at all.
#             -> survey_assignments.last_week_reset
#
# Because of that split, ``video_upload_time - video_pending_start_time`` is a
# correct duration (both UTC_WALL) but ``end_time - start_time`` is also correct
# (both IST_WALL) - while comparing across the two groups is not. The portal does
# exactly that in a few places; we reproduce its arithmetic verbatim in
# ``core.derive`` rather than silently "fixing" it.
#
# Separately, the HTML adds +5:30 to ``start_time`` for display, which for an
# IST_WALL column renders IST+5:30 - i.e. 5h30m ahead of reality. That is a
# pre-existing bug. ``legacy_display()`` reproduces it on purpose so the API can
# serve dashboard payloads that match the portal screen exactly, while the
# ``*_utc`` / ``*_ist`` fields tell the truth.
# ==========================================================================

from datetime import date, datetime, timedelta, timezone

# (merged) now defined in this module: IST_OFFSET

# Semantic tags
IST_WALL = "ist_wall"
UTC_WALL = "utc_wall"
DATE_ONLY = "date_only"

UTC = timezone.utc
IST = timezone(IST_OFFSET)

#: (table, column) -> semantic.  Anything not listed defaults to UTC_WALL.
COLUMN_SEMANTICS = {
    ("surveys", "start_time"): IST_WALL,
    ("surveys", "end_time"): IST_WALL,
    ("surveys", "video_pending_start_time"): UTC_WALL,
    ("surveys", "video_upload_time"): UTC_WALL,
    # These three switched from datetime.utcnow() to
    # datetime.now(pytz.timezone("Asia/Kolkata")) in routes/teamleader.py
    # (toggle_task1 / toggle_task2 / toggle_survey_form). The column is a naive
    # DateTime, so the tz-aware value lands as IST wall time. Reporting them as
    # UTC would put every team-leader timestamp 5h30m in the past.
    ("surveys", "survey_form_completed_at"): IST_WALL,
    ("surveys", "task1_completed_at"): IST_WALL,
    ("surveys", "task2_completed_at"): IST_WALL,
    ("surveys", "roadvision_completed_at"): UTC_WALL,
    # utils/video_count_sync.py writes this with datetime.utcnow().
    ("surveys", "video_count_checked_at"): UTC_WALL,
    # Never written by any current route; assumed UTC.  Reported as
    # semantic_confidence="assumed" by GET /api/v1/meta/version.
    ("survey_assignments", "deadline_time"): UTC_WALL,
    ("survey_assignments", "last_week_reset"): DATE_ONLY,
    # No `deleted_at` entries: soft delete is a tombstone in api_deleted_records,
    # not a column on these tables.  api_deleted_records.deleted_at is written
    # with datetime.utcnow() and is covered by the api_* defaults.
}

ASSUMED_SEMANTICS = {("survey_assignments", "deadline_time")}


def semantic_for(table, column):
    """Return the tz semantic for a column, defaulting to UTC_WALL."""
    return COLUMN_SEMANTICS.get((table, column), UTC_WALL)


# ---------------------------------------------------------------------------
# "now"
# ---------------------------------------------------------------------------


def utc_now():
    """Naive UTC, matching how the portal writes most timestamps."""
    return datetime.utcnow()


def ist_now():
    """
    Naive IST wall-clock.

    Reproduces ``datetime.utcnow() + timedelta(hours=5, minutes=30)``
    (routes/admin.py:37, regional.py, teamleader.py, roadvision.py).
    """
    return datetime.utcnow() + IST_OFFSET


def ist_now_aware():
    """Timezone-aware IST, matching ``datetime.now(pytz.timezone(...))``."""
    return datetime.now(IST)


# ---------------------------------------------------------------------------
# Week boundaries - two different conventions, deliberately kept apart
# ---------------------------------------------------------------------------


def week_start_monday(ist_dt=None):
    """
    Midnight on the Monday of ``ist_dt``'s week, in IST wall-clock.

    Used by the missed-survey engine (routes/admin.py:46-55) to decide whether a
    survey already exists "this week".
    """
    ist_dt = ist_dt or ist_now()
    return (ist_dt - timedelta(days=ist_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def week_start_sunday(dt=None):
    """
    Midnight on the Sunday on/before ``dt``.

    Used by the weekly-duplicate guard (routes/captain.py:179-191), which - unlike
    the alert engine - treats Sunday as the first day of the week.

    Do NOT "unify" this with week_start_monday(); the two guards genuinely differ
    and collapsing them changes which surveys are blocked.
    """
    dt = dt or utc_now()
    # Python: Monday=0 .. Sunday=6.  Sunday must map to an offset of 0.
    days_since_sunday = (dt.weekday() + 1) % 7
    return (dt - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def as_utc(value, semantic=UTC_WALL):
    """Naive stored value -> aware UTC datetime (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        if semantic == IST_WALL:
            return (value - IST_OFFSET).replace(tzinfo=UTC)
        return value.replace(tzinfo=UTC)
    return None


def as_ist(value, semantic=UTC_WALL):
    """Naive stored value -> aware IST datetime (or None)."""
    utc_value = as_utc(value, semantic)
    return utc_value.astimezone(IST) if utc_value else None


def legacy_display(value):
    """
    Reproduce the portal's display arithmetic: ``value + 5:30``.

    For an IST_WALL column this is 5h30m ahead of the true instant - a
    pre-existing bug. Reproduced deliberately so dashboard payloads match the
    rendered page. Never used on raw resource endpoints.
    """
    if value is None:
        return None
    return value + IST_OFFSET


# ---------------------------------------------------------------------------
# ISO formatting
# ---------------------------------------------------------------------------


def iso(value):
    """Aware datetime -> ISO-8601 string, with 'Z' for UTC. None-safe."""
    if value is None:
        return None
    if isinstance(value, datetime):
        text = value.isoformat()
        return text.replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return None


def iso_naive(value):
    """Naive datetime -> ISO-8601 string with no offset, exactly as stored."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def serialize_datetime(value, semantic=UTC_WALL):
    """
    Return the three-key representation used across the API.

    ``{"utc": ..., "ist": ..., "raw": ...}`` - callers flatten this into
    ``<field>_utc`` / ``<field>_ist`` / ``<field>_raw``.
    """
    return {
        "utc": iso(as_utc(value, semantic)),
        "ist": iso(as_ist(value, semantic)),
        "raw": iso_naive(value),
    }


# ---------------------------------------------------------------------------
# Parsing incoming values
# ---------------------------------------------------------------------------


class TimeParseError(ValueError):
    """Raised when an incoming datetime string cannot be understood."""


def parse_datetime(value, semantic=UTC_WALL, assume_tz=None):
    """
    Parse an incoming ISO-8601 string into the naive form this column stores.

    Rules:
      * Offset-bearing input is converted to the column's own semantic, so it
        round-trips exactly.
      * Naive input is interpreted *as the column's semantic* by default - so
        posting ``start_time: "2026-07-28T10:00:00"`` means 10:00 IST, matching
        how the portal writes it.  Pass ``assume_tz="UTC"`` to override.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise TimeParseError(f"'{value}' is not a valid ISO-8601 datetime")

    if parsed.tzinfo is None:
        if assume_tz and assume_tz.upper() == "UTC":
            parsed = parsed.replace(tzinfo=UTC)
        elif assume_tz and assume_tz.upper() in ("IST", "ASIA/KOLKATA"):
            parsed = parsed.replace(tzinfo=IST)
        else:
            # Interpret in the column's own convention -> already correct on disk.
            return parsed

    if semantic == IST_WALL:
        return parsed.astimezone(IST).replace(tzinfo=None)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def parse_date(value):
    """Parse ``YYYY-MM-DD`` (or a full ISO datetime) into a ``date``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        raise TimeParseError(f"'{value}' is not a valid date (expected YYYY-MM-DD)")


def parse_date_boundary(value, semantic=UTC_WALL, end=False):
    """
    Turn a ``YYYY-MM-DD`` filter value into the naive datetime to compare against.

    ``end=True`` returns midnight of the *next* day, reproducing the portal's
    exclusive upper bound (routes/admin.py:167).

    The returned value is in the column's own wall-clock, so filtering
    ``start_time`` (IST_WALL) by ``from_date=2026-07-28`` means IST midnight -
    matching what the HTML page does.
    """
    day = parse_date(value)
    if day is None:
        return None
    boundary = datetime(day.year, day.month, day.day)
    if end:
        boundary += timedelta(days=1)
    return boundary

# ==========================================================================
# weeks
# Project-week arithmetic.
#
# Week N runs ``PROJECT_START + (N-1)*7 days`` up to (but not including) 7 days
# later, and is applied to ``Survey.start_time``.
#
# Reproduces routes/admin.py:146-157 / 196-200 (and the identical copies in
# regional.py:102-115, teamleader.py:91-106, roadvision.py:85-100).
# ==========================================================================

from datetime import datetime, timedelta

# (merged) now defined in this module: PROJECT_START


def week_window(week_no, project_start=None):
    """
    Return ``(start, end)`` for a 1-based project week.

    ``end`` is exclusive, matching ``Survey.start_time < end``.
    """
    week_no = int(week_no)
    if week_no < 1:
        raise ValueError("week must be >= 1")
    start = (project_start or PROJECT_START) + timedelta(days=(week_no - 1) * 7)
    return start, start + timedelta(days=7)


def current_week_number(now=None, project_start=None):
    """
    Highest week number that has begun.

    Mirrors routes/admin.py:196-198 exactly, including its use of
    ``datetime.utcnow()`` (not IST) and integer division on whole dates.
    """
    now = now or datetime.utcnow()
    start = project_start or PROJECT_START
    return ((now.date() - start.date()).days // 7) + 1


def week_list(now=None, project_start=None):
    """
    ``[1, 2, ... current]`` - the week dropdown every dashboard renders.

    Returns ``[]`` before the project start rather than a negative range.
    """
    total = current_week_number(now, project_start)
    if total < 1:
        return []
    return list(range(1, total + 1))


def week_of(value, project_start=None):
    """Which project week a datetime falls in, or None if before the start."""
    if value is None:
        return None
    start = project_start or PROJECT_START
    delta_days = (value.date() - start.date()).days
    if delta_days < 0:
        return None
    return (delta_days // 7) + 1


def week_detail(week_no, now=None, project_start=None):
    """Descriptor for ``GET /api/v1/meta/weeks``."""
    from core.config import IST_WALL, iso, as_ist, as_utc

    start, end = week_window(week_no, project_start)
    current = current_week_number(now, project_start)
    return {
        "week": int(week_no),
        # start_time is an IST_WALL column, so these bounds are IST wall-clock.
        "start_raw": start.isoformat(),
        "end_raw": end.isoformat(),
        "start_utc": iso(as_utc(start, IST_WALL)),
        "end_utc": iso(as_utc(end, IST_WALL)),
        "start_ist": iso(as_ist(start, IST_WALL)),
        "end_ist": iso(as_ist(end, IST_WALL)),
        "is_current": int(week_no) == current,
    }

# ==========================================================================
# checklists
# The two checklists the captain app renders.
#
# Both are hardcoded HTML in the templates and never persisted, so the only way
# another site can show them is to read them from here.
#
#   PRE_SURVEY_CHECKLIST  20 items, templates/captain/checklist.html:51-149
#   RECORDING_CHECKLIST    7 items, templates/captain/recording.html:154-224
#
# Transcribed verbatim.  If you edit the templates, edit these too.
# ==========================================================================

PRE_SURVEY_CHECKLIST = (
    "Placement of the dashcam inside the vehicle : Center of the windshield",
    "Firmly fixed on the windshield",
    "Dashcam power is ON and properly connected to vehicle power",
    "SD card inserted in the dashcam and formatted",
    "GPS of the dashcam is working properly",
    "Dashcam camera lens is clean",
    "Resolution should be 1080p",
    "Loop recording setup : 5 minutes",
    "Dashcam camera alignment : 0 degrees",
    "No bonnet should be visible in the dashcam camera sight",
    "Road should be visible in 70-80% of the frame",
    "Sky should be maximum 20-25% of the frame",
    "Date and time of dashcam are correct",
    "Powerbank is fully charged",
    "Good streetlights / good car headlights available during night survey",
    "No heavy rain, fog, or poor visibility conditions",
    "Record a test clip",
    "Check the test clip on phone preview using the app",
    "Upload test clip",
    "Wait for GO confirmation from RoadVision team",
)

RECORDING_CHECKLIST = (
    "If it is sunrise time, avoid moving towards the East while recording",
    "If it is sunset time, avoid moving towards the West while recording",
    "If it is a 2-lane or 4-lane road, drive in the left-most lane",
    "If it is a 6-lane road, drive in the middle lane",
    "If it is an 8-lane road, drive in the second lane from the left-most lane",
    "Maintain approximately 20 meters distance from the vehicle ahead",
    "Vehicle speed while driving should be around 40 km/hr",
)

CHECKLIST_KINDS = ("pre_survey", "recording")


def as_items(texts):
    """``("a", "b")`` -> ``[{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]``."""
    return [{"id": i, "text": text} for i, text in enumerate(texts, start=1)]


def get_checklist(kind):
    """Return one checklist as a list of items, or None for an unknown kind."""
    if kind == "pre_survey":
        return as_items(PRE_SURVEY_CHECKLIST)
    if kind == "recording":
        return as_items(RECORDING_CHECKLIST)
    return None


def all_checklists():
    return {
        "pre_survey": as_items(PRE_SURVEY_CHECKLIST),
        "recording": as_items(RECORDING_CHECKLIST),
    }

# ==========================================================================
# derive
# Values the portal computes at render time and never stores.
#
# Without these the other site cannot reproduce a single dashboard, because none
# of them exist as columns: team KM totals, upload durations, scheduled day,
# survey duration, and the CASE expressions that give each dashboard its row
# order.
#
# Every function documents the route code it mirrors and reproduces it exactly -
# including the quirks.  Model imports are done lazily inside functions so this
# module stays cheap to import and free of circular-import risk.
# ==========================================================================

from datetime import datetime

# (merged) now defined in this module: SURVEY_COMPLETED, SURVEY_GROUNDWORK_COMPLETED, SURVEY_ONGOING, SURVEY_VIDEO_PENDING, TEAMS, UPLOAD_DURATION_RED_MINUTES, WEEKDAY_ORDER_ELSE, SCHEDULE_DAYS, team_for_state,
# (merged) now defined in this module: utc_now

# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------


def upload_duration(survey, now=None):
    """
    ``(minutes, status_text)`` for the "Upload Duration" column.

    Verbatim port of routes/admin.py:366-402 (and the identical blocks in
    regional.py:177-208, teamleader.py:185-211, roadvision.py:173-199).

    Both timestamps are UTC_WALL, so this arithmetic is sound.
    """
    now = now or utc_now()

    if survey.status == SURVEY_VIDEO_PENDING and survey.video_pending_start_time:
        minutes = int((now - survey.video_pending_start_time).total_seconds() / 60)
        return minutes, "Upload Pending"

    if survey.video_pending_start_time and survey.video_upload_time:
        minutes = int(
            (survey.video_upload_time - survey.video_pending_start_time).total_seconds()
            / 60
        )
        return minutes, "Upload Duration"

    return 0, ""


def survey_duration_minutes(survey):
    """
    Elapsed survey time in whole minutes, or None.

    Mirrors the Jinja expression
    ``((end_time - start_time).total_seconds() / 60) | int``.
    Both columns are IST_WALL, so the subtraction is valid.
    """
    if not survey.start_time or not survey.end_time:
        return None
    return int((survey.end_time - survey.start_time).total_seconds() / 60)


def is_upload_overdue(minutes):
    """The templates bold/redden anything past 480 minutes."""
    return bool(minutes and minutes > UPLOAD_DURATION_RED_MINUTES)


# ---------------------------------------------------------------------------
# Team KM totals
# ---------------------------------------------------------------------------


def team_km_totals(surveys):
    """
    Sum ``section_length`` over *completed* surveys, bucketed by team.

    Port of routes/admin.py:258-290.  Non-completed rows are skipped and a NULL
    section_length counts as 0, exactly as the original does.

    Returns ``{"Krish": x, "Godbole": y, "Aspizo": z, "total": t}`` with each
    value rounded to 2dp (the route rounds only at render time; rounding here is
    equivalent because it rounds the same final sums).
    """
    totals = {key: 0.0 for key in TEAMS}

    for survey in surveys:
        if survey.status != SURVEY_COMPLETED:
            continue
        km = survey.section_length or 0
        team = team_for_state(survey.state)
        if team:
            totals[team] += km

    result = {key: round(value, 2) for key, value in totals.items()}
    result["total"] = round(sum(totals.values()), 2)
    return result


def report_summary(surveys):
    """
    The four report stat cards.

    Port of routes/admin.py:872-898.  ``total_hours`` sums only rows that have
    both timestamps.
    """
    total_minutes = 0.0
    for survey in surveys:
        if survey.start_time and survey.end_time:
            total_minutes += (survey.end_time - survey.start_time).total_seconds() / 60

    return {
        "completed_surveys": len(surveys),
        "total_km": round(sum((s.section_length or 0) for s in surveys), 2),
        "captains": len({s.captain_email for s in surveys}),
        "total_hours": round(total_minutes / 60, 2),
    }


# ---------------------------------------------------------------------------
# Scheduled day  (batched replacement for the N+1 loops)
# ---------------------------------------------------------------------------


def scheduled_day_map(section_nos):
    """
    ``{section_no: survey_day}`` resolved in ONE query.

    Replaces the per-row lookup at routes/admin.py:297-306 (repeated in
    teamleader.py:158-167 and roadvision.py:146-155), which issues one query per
    survey.

    Behaviour note: the original uses ``.first()`` with no ORDER BY, so when two
    assignments share a section_no the winner is arbitrary. This orders by ``id``
    and takes the lowest, which is deterministic. On a dataset where section_no
    is unique in survey_assignments the two are identical - check with::

        SELECT section_no, count(*) FROM survey_assignments
        GROUP BY 1 HAVING count(*) > 1;
    """
    from models.db_models import SurveyAssignment

    wanted = {s for s in section_nos if s}
    if not wanted:
        return {}

    rows = (
        SurveyAssignment.query.with_entities(
            SurveyAssignment.section_no, SurveyAssignment.survey_day
        )
        .filter(SurveyAssignment.section_no.in_(wanted))
        .order_by(SurveyAssignment.id)
        .all()
    )

    mapping = {}
    for section_no, survey_day in rows:
        mapping.setdefault(section_no, survey_day)
    return mapping


def attach_derived(surveys, now=None, include_scheduled_day=True):
    """
    Set the attributes the dashboard templates read off each survey object.

    Sets ``scheduled_day``, ``display_start_time``, ``display_end_time``,
    ``upload_duration_minutes``, ``upload_status_text`` and
    ``survey_duration_minutes`` - the same names the Jinja templates use, so a
    dashboard payload can be built by simply serializing the result.

    ``display_*`` intentionally reproduces the portal's ``+5:30`` display bug
    (see core.timeutils.legacy_display).
    """
    from core.config import legacy_display

    surveys = list(surveys)
    now = now or utc_now()

    day_map = {}
    if include_scheduled_day:
        day_map = scheduled_day_map(s.section_no for s in surveys)

    for survey in surveys:
        if include_scheduled_day:
            survey.scheduled_day = day_map.get(survey.section_no) or survey.survey_day
        survey.display_start_time = legacy_display(survey.start_time)
        survey.display_end_time = legacy_display(survey.end_time)
        minutes, text = upload_duration(survey, now)
        survey.upload_duration_minutes = minutes
        survey.upload_status_text = text
        survey.survey_duration_minutes = survey_duration_minutes(survey)

    return surveys


# ---------------------------------------------------------------------------
# Ordering  (the CASE expressions each dashboard uses)
# ---------------------------------------------------------------------------


def admin_rank_case():
    """
    6-tier ordering of the admin dashboard - routes/admin.py:204-251.

    ongoing(1) -> groundwork(2) -> video_pending(3) -> completed-with-a-task-open(4)
    -> completed-and-done(5) -> anything else(6).
    """
    from sqlalchemy import and_, case, or_

    from models.db_models import Survey

    return case(
        (Survey.status == SURVEY_ONGOING, 1),
        (Survey.status == SURVEY_GROUNDWORK_COMPLETED, 2),
        (Survey.status == SURVEY_VIDEO_PENDING, 3),
        (
            and_(
                Survey.status == SURVEY_COMPLETED,
                or_(
                    Survey.task1_completed == False,  # noqa: E712
                    Survey.task2_completed == False,  # noqa: E712
                    Survey.survey_form_completed == False,  # noqa: E712
                ),
            ),
            4,
        ),
        (
            and_(
                Survey.status == SURVEY_COMPLETED,
                Survey.task1_completed == True,  # noqa: E712
                Survey.task2_completed == True,  # noqa: E712
                Survey.survey_form_completed == True,  # noqa: E712
            ),
            5,
        ),
        else_=6,
    )


def regional_rank_case():
    """Simple status ordering - routes/regional.py:140-149."""
    from sqlalchemy import case

    from models.db_models import Survey

    return case(
        (Survey.status == SURVEY_ONGOING, 1),
        (Survey.status == SURVEY_GROUNDWORK_COMPLETED, 2),
        (Survey.status == SURVEY_VIDEO_PENDING, 3),
        (Survey.status == SURVEY_COMPLETED, 4),
        else_=5,
    )


def teamleader_rank_case():
    """
    Three-band ordering used by the team-leader dashboard - mirrors
    routes/teamleader.py:136-160.

    The rule changed: it used to float *anything* outstanding to the top. Now the
    top band is specifically "survey form done, raw video done, final report
    still outstanding" - the rows a team leader can actually action - while
    fully-done rows sink and everything else sits in the middle.

        1  form YES + task1 YES + task2 NO   -> top, actionable
        2  anything else (including NO/NO/NO) -> middle
        3  all three YES                      -> bottom, finished
    """
    from sqlalchemy import and_, case

    from models.db_models import Survey

    return case(
        (
            and_(
                Survey.survey_form_completed == True,  # noqa: E712
                Survey.task1_completed == True,  # noqa: E712
                Survey.task2_completed == False,  # noqa: E712
            ),
            1,
        ),
        (
            and_(
                Survey.survey_form_completed == True,  # noqa: E712
                Survey.task1_completed == True,  # noqa: E712
                Survey.task2_completed == True,  # noqa: E712
            ),
            3,
        ),
        else_=2,
    )


def roadvision_rank_case():
    """Unreviewed first - routes/roadvision.py:126-140."""
    from sqlalchemy import case

    from models.db_models import Survey

    return case(
        (Survey.roadvision_completed == False, 1),  # noqa: E712
        (Survey.roadvision_completed == True, 2),  # noqa: E712
        else_=3,
    )


def day_order_case():
    """Mon=1 .. Fri=5, anything else 6 - routes/admin.py:688-695."""
    from sqlalchemy import case

    from models.db_models import SurveyAssignment

    return case(
        *[
            (SurveyAssignment.survey_day == day, index)
            for index, day in enumerate(SCHEDULE_DAYS, start=1)
        ],
        else_=WEEKDAY_ORDER_ELSE,
    )


# ---------------------------------------------------------------------------
# Schedule day counts
# ---------------------------------------------------------------------------


def day_counts(assignments):
    """
    ``{"monday_count": n, ..., "total_count": n}`` from an in-memory list.

    This is the regional/teamleader approach (regional.py:427-431), which counts
    the already-filtered rows.  The admin page instead runs 5 unfiltered COUNT
    queries (admin.py:707-725), so its cards ignore the active filters - use
    :func:`day_counts_global` to reproduce that.
    """
    counts = {
        f"{day.lower()}_count": sum(1 for a in assignments if a.survey_day == day)
        for day in SCHEDULE_DAYS
    }
    counts["total_count"] = len(assignments)
    return counts


def day_counts_global():
    """
    Unfiltered per-day counts, reproducing routes/admin.py:707-725.

    Deliberately ignores any active filter, because the admin schedules page
    does. One grouped query instead of the original five.
    """
    from extensions import db
    from models.db_models import SurveyAssignment

    rows = (
        db.session.query(SurveyAssignment.survey_day, db.func.count(SurveyAssignment.id))
        .group_by(SurveyAssignment.survey_day)
        .all()
    )
    by_day = dict(rows)
    counts = {f"{day.lower()}_count": int(by_day.get(day, 0)) for day in SCHEDULE_DAYS}
    counts["total_count"] = int(sum(by_day.values()))
    return counts


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def survey_ref_id(survey, when=None):
    """
    The human survey reference used in email subjects.

    ``{upc_code}_{cycle_no:03d}_{ddmmyy}`` - utils/email_templates.py.
    """
    when = when or survey.start_time
    stamp = when.strftime("%d%m%y") if isinstance(when, datetime) else ""
    cycle = survey.cycle_no or 1
    return f"{survey.upc_code}_{cycle:03d}_{stamp}"


def defect_counts(survey):
    """The 8 video counts, split into LHS/RHS blocks for display."""
    from core.config import DEFECT_FIELD_META

    lhs, rhs = {}, {}
    for field, (side, label) in DEFECT_FIELD_META.items():
        target = lhs if side == "LHS" else rhs
        target[field] = {"label": label, "value": getattr(survey, field, 0) or 0}
    return {"lhs": lhs, "rhs": rhs}

