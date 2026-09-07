"""
Automated RoadVision remark generation.

Compares the captain-entered video counts on a Survey against the authoritative
per-bucket counts from the RoadVision ops API, and writes survey.roadvision_remark.

Matching rule
-------------
The bucket may legitimately hold MORE videos than were enlisted on the portal
(duplicate or trailing clips). It must never hold FEWER. So a survey passes when
every cell satisfies  portal <= bucket ; a cell where  portal > bucket  is a
shortfall and means videos are missing from GCS.

See VIDEO_COUNTS_API.md for the upstream contract.
"""

import base64
import binascii
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from extensions import db
from models.db_models import Survey

log = logging.getLogger(__name__)

API_URL = os.getenv(
    "RV_VIDEO_COUNTS_URL",
    "http://34.180.38.204:7001/api/video-counts",
)

# Upstream bucket scan runs every 30s, so polling faster returns identical data.
CACHE_TTL_SECONDS = 30

# Typical response is ~45ms, but this is a single unmanaged VM with no failover.
FETCH_TIMEOUT_SECONDS = 15

# The on-completion hook runs inside the captain's own request, on a phone, in
# the field. Fifteen seconds of dead air there is worse than no remark: the
# nightly cron re-checks the same survey anyway, so give up early and let it.
INLINE_FETCH_TIMEOUT_SECONDS = 4

DEFAULT_WINDOW_DAYS = 10

MATCH_REMARK = (
    "The number of uploaded videos in the GCP bucket matches the enlisted "
    "video count, and all videos represent the complete surveyed length."
)

# Survey column  ->  (API road type, API side, human label)
FIELD_MAP = [
    ("mcw_lhs_count", "MCW", "LHS", "Main Carriageway LHS"),
    ("mcw_rhs_count", "MCW", "RHS", "Main Carriageway RHS"),
    ("service_lhs_count", "SR", "LHS", "Service Road LHS"),
    ("service_rhs_count", "SR", "RHS", "Service Road RHS"),
    ("slip_lhs_count", "SL", "LHS", "Slip Road LHS"),
    ("slip_rhs_count", "SL", "RHS", "Slip Road RHS"),
    ("ir_lhs_count", "IR", "LHS", "Intersection Road LHS"),
    ("ir_rhs_count", "IR", "RHS", "Intersection Road RHS"),
]

#: Warn this far ahead of RV_DASHBOARD_TOKEN expiring.
EXPIRY_WARNING_DAYS = 30

_cache = {"fetched_at": 0.0, "rows": None}


# ---------------------------------------------------------------------------
# Token expiry
# ---------------------------------------------------------------------------

def token_expiry(token=None):
    """Expiry of RV_DASHBOARD_TOKEN as a naive UTC datetime, or None.

    Reads the `exp` claim without verifying the signature - we are not
    authenticating anything here, just reading a date off a token we were handed.
    Verification is the upstream's job and we don't have its key anyway.
    """

    raw = token if token is not None else os.getenv("RV_DASHBOARD_TOKEN")

    if not raw:
        return None

    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")

        if not exp:
            return None

        # Epoch + timedelta rather than utcfromtimestamp, which is deprecated on
        # 3.12+. Result is naive UTC, matching the rest of this module.
        return datetime(1970, 1, 1) + timedelta(seconds=int(exp))

    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None


def check_token_expiry():
    """Log and describe how close the token is to expiring.

    The upstream answers an expired token with a plain 401 and no warning, so
    without this the first sign of trouble is the sync silently doing nothing.
    Returns a dict for the summary, or None if there is nothing to report.
    """

    expires_at = token_expiry()

    if expires_at is None:
        return None

    days_left = (expires_at - datetime.utcnow()).days

    status = {
        "token_expires_at": expires_at.isoformat() + "Z",
        "token_days_remaining": days_left,
    }

    if days_left < 0:
        log.error(
            "RV_DASHBOARD_TOKEN EXPIRED on %s - the upstream will answer 401. "
            "A replacement must come from the RoadVision side.",
            expires_at.date(),
        )
        status["token_expired"] = True

    elif days_left <= EXPIRY_WARNING_DAYS:
        log.warning(
            "RV_DASHBOARD_TOKEN expires in %s day(s), on %s. Request a "
            "replacement from the RoadVision side now.",
            days_left,
            expires_at.date(),
        )
        status["token_expiring_soon"] = True

    return status


# ---------------------------------------------------------------------------
# Upstream fetch
# ---------------------------------------------------------------------------

def fetch_counts(force=False, timeout=None):
    """Return the API's rows list, or None if it can't be obtained.

    Caches for 30s and serves the last good response on failure, because the
    upstream is a single VM with no HA - a slightly stale table beats no table.

    `timeout` overrides FETCH_TIMEOUT_SECONDS for callers that cannot afford to
    wait - see INLINE_FETCH_TIMEOUT_SECONDS.
    """

    token = os.getenv("RV_DASHBOARD_TOKEN")

    if not token:
        log.error("RV_DASHBOARD_TOKEN is not set - skipping video count sync")
        return None

    fresh = (time.time() - _cache["fetched_at"]) < CACHE_TTL_SECONDS

    if _cache["rows"] is not None and fresh and not force:
        return _cache["rows"]

    request = urllib.request.Request(
        API_URL,
        headers={"Authorization": "Bearer " + token},
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=timeout or FETCH_TIMEOUT_SECONDS
        ) as response:

            payload = json.loads(response.read())

        _cache["rows"] = payload["rows"]
        _cache["fetched_at"] = time.time()

        log.info(
            "video-counts fetched: %s rows, last scan %s",
            payload.get("row_count"),
            payload.get("last_scan_completed"),
        )

        return _cache["rows"]

    except urllib.error.HTTPError as exc:

        body = exc.read()[:200].decode(errors="replace")
        log.error("video-counts upstream %s: %s", exc.code, body)

    except Exception as exc:

        log.error("video-counts fetch failed: %s: %s", type(exc).__name__, exc)

    # Serve stale rather than fail.
    if _cache["rows"] is not None:
        log.warning("serving stale video-counts from cache")
        return _cache["rows"]

    return None


# ---------------------------------------------------------------------------
# Matching a Survey to an API row
# ---------------------------------------------------------------------------

def _normalise(value):
    """Strip separators so N/09064/02001/JH and N_09064_02001_JH compare equal."""

    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def build_index(rows):
    """Index API rows by (section, cycle) and by (normalised section, cycle).

    (section, cycle) is unique upstream - verified against all 204 live rows.
    """

    by_section = {}
    by_normalised = {}

    for row in rows:

        cycle = row["cycle"]

        by_section[(row["section"], cycle)] = row
        by_normalised[(_normalise(row["section"]), cycle)] = row

    return {"section": by_section, "normalised": by_normalised}


def section_candidates(raw):
    """Section keys to try for a free-text section_no, and whether it is ambiguous.

    `section_no` is free text on this schema, and real values include '84(B)',
    '133 re-survey', '1 & 2' and full UPC codes. A plain zfill(3) matches none of
    those, which is why ~18 surveys were reported as having no bucket data when
    the data was there all along.

    Returns (candidates, ambiguous). `ambiguous` is True when the value names
    more than one distinct section - '1 & 2' covers two stretches with two
    separate bucket rows, and silently picking one would produce a confidently
    wrong verdict. Those are declined instead.
    """

    text = str(raw or "").strip()

    if not text:
        return [], False

    candidates = [text]

    if text.zfill(3) != text:
        candidates.append(text.zfill(3))

    # Distinct digit runs, in order: '84(B)' -> ['84'], '1 & 2' -> ['1', '2'].
    runs = []
    for run in re.findall(r"\d+", text):
        stripped = run.lstrip("0") or "0"
        if stripped not in runs:
            runs.append(stripped)

    # Several numbers alone do not make a value ambiguous - a UPC code like
    # 'N/09064/02001/JH' is one identifier with four of them, and belongs on the
    # UPC fallback path. Ambiguity needs an explicit conjunction: '1 & 2'.
    ambiguous = len(runs) > 1 and bool(re.search(r"[&+,]|\band\b", text, re.I))

    if len(runs) == 1:
        for form in (runs[0], runs[0].zfill(3)):
            if form not in candidates:
                candidates.append(form)

    return candidates, ambiguous


def section_parts(raw):
    """The distinct sections named by a multi-section value.

    '1 & 2' -> ['1', '2'];  '7 & 33' -> ['7', '33'].  Order is preserved so the
    remark reads the same way the captain wrote it.
    """

    parts = []

    for run in re.findall(r"\d+", str(raw or "")):

        stripped = run.lstrip("0") or "0"

        if stripped not in parts:
            parts.append(stripped)

    return parts


def lookup_section(section, cycle, index):
    """The API row for one section key at one cycle, or None."""

    for form in (section, section.zfill(3)):

        row = index["section"].get((form, cycle))

        if row:
            return row

        row = index["normalised"].get((_normalise(form), cycle))

        if row:
            return row

    return None


def combine_rows(rows):
    """Sum several API rows into one synthetic row.

    A survey covering '1 & 2' enlists its videos across both sections, and the
    bucket carries a separate row for each. Under this module's rule - the
    bucket may hold more than was enlisted but never fewer - the figure to
    compare against is the total across every section the survey covers.

    Only ever called when *every* named section resolved. Summing a partial set
    would understate the bucket and manufacture a shortfall that isn't real.
    """

    counts = {}

    for row in rows:

        for road_type, sides in row["counts"].items():

            bucket = counts.setdefault(road_type, {})

            for side, value in sides.items():
                bucket[side] = bucket.get(side, 0) + (value or 0)

    return {
        "section": " + ".join(str(row["section"]) for row in rows),
        "cycle": rows[0]["cycle"],
        "counts": counts,
        "total": sum(row.get("total") or 0 for row in rows),
        "combined_from": [row["section"] for row in rows],
    }


def match_survey(survey, index):
    """Resolve a survey to the bucket counts it should be judged against.

    Returns a dict with:
        row       the API row, a combined synthetic row, or None
        ambiguous True when section_no named sections that could not be resolved
        combined  the upstream section keys that were summed, if more than one
        missing   the named sections that have no upstream row

    Primary key is the zero-padded section number. Some sections are carried
    upstream as the UPC code instead, so fall back to a normalised UPC compare.
    """

    blank = {"row": None, "ambiguous": False, "combined": None, "missing": []}

    if survey.cycle_no is None:
        return blank

    cycle = "WC{}".format(survey.cycle_no)

    candidates, ambiguous = section_candidates(survey.section_no)

    if not ambiguous:

        for candidate in candidates:

            row = index["section"].get((candidate, cycle))

            if row:
                return dict(blank, row=row)

            row = index["normalised"].get((_normalise(candidate), cycle))

            if row:
                return dict(blank, row=row)

    else:

        # A multi-section survey is answerable whenever every section it names
        # has a bucket row: compare the enlisted total against their sum. When
        # one is absent the honest answer is still "cannot verify", but we can
        # now say *which* section is missing instead of asking for a manual
        # check with no further detail.
        parts = section_parts(survey.section_no)

        found = []
        missing = []

        for part in parts:

            row = lookup_section(part, cycle, index)

            if row:
                found.append(row)
            else:
                missing.append(part)

        if found and not missing:

            return {
                "row": combine_rows(found),
                "ambiguous": False,
                "combined": [row["section"] for row in found],
                "missing": [],
            }

        blank = dict(blank, missing=missing)

    row = index["normalised"].get((_normalise(survey.upc_code), cycle))

    if row:
        return dict(blank, row=row, ambiguous=False, missing=[])

    return dict(blank, ambiguous=ambiguous)


def match_row(survey, index):
    """The API row for this survey, or None. See match_survey."""

    return match_survey(survey, index)["row"]


def match_row_detailed(survey, index):
    """As match_row, but also reports whether the section could not be resolved."""

    result = match_survey(survey, index)

    return result["row"], result["ambiguous"]


# ---------------------------------------------------------------------------
# Comparison + remark text
# ---------------------------------------------------------------------------

def find_shortfalls(survey, api_row):
    """Cells where the portal enlisted more videos than the bucket holds.

    Returns a list of (label, enlisted, in_bucket). An empty list means pass -
    surplus videos in the bucket are acceptable and are not reported.
    """

    shortfalls = []

    counts = api_row.get("counts") or {}

    for column, road_type, side, label in FIELD_MAP:

        enlisted = getattr(survey, column) or 0

        # .get rather than [][]: a road type or side absent from the payload
        # means the bucket holds none of them, which is a shortfall to report -
        # not a KeyError that takes down the whole sync run for every survey.
        in_bucket = (counts.get(road_type) or {}).get(side) or 0

        if enlisted > in_bucket:
            shortfalls.append((label, enlisted, in_bucket))

    return shortfalls


def build_remark(
    survey,
    api_row,
    shortfalls,
    ambiguous=False,
    missing=None,
    combined=None,
):
    """The remark text for this survey's comparison outcome."""

    if ambiguous:

        # Name the sections that have no bucket row. "Section 2 has no videos"
        # is something the RoadVision team can act on; the old wording - split
        # the survey, or check by hand - told them nothing they did not know.
        if missing:

            return (
                "The video count could not be verified automatically: section "
                "'{}' covers more than one section, and no videos were found "
                "in the GCP bucket for section {} (cycle WC{}). Please confirm "
                "those videos were uploaded under the correct section and "
                "cycle, then re-run the check.".format(
                    survey.section_no,
                    ", ".join(missing),
                    survey.cycle_no,
                )
            )

        return (
            "The video count could not be verified automatically: section "
            "'{}' names more than one section, and each has its own set of "
            "videos in the GCP bucket. Please split this survey by section, or "
            "check the count by hand.".format(survey.section_no)
        )

    if api_row is None:

        return (
            "No video data was found in the GCP bucket for section "
            "{} (cycle WC{}). The enlisted video count could not be verified - "
            "please confirm the videos were uploaded under the correct section "
            "and cycle.".format(
                survey.section_no or "-",
                survey.cycle_no,
            )
        )

    # Say so when the verdict rests on several sections added together, so the
    # figures in the remark can be traced back to the bucket.
    basis = ""

    if combined and len(combined) > 1:
        basis = (
            " This survey covers sections {}; the check was made against the "
            "combined video count for all of them.".format(", ".join(combined))
        )

    if not shortfalls:
        return MATCH_REMARK + basis

    details = "; ".join(
        "{} (enlisted {}, in bucket {}, short by {})".format(
            label,
            enlisted,
            in_bucket,
            enlisted - in_bucket,
        )
        for label, enlisted, in_bucket in shortfalls
    )

    return (
        "Video count mismatch - the GCP bucket holds fewer videos than were "
        "enlisted for: {}. Please check the GCS upload for this section and "
        "re-verify the video count.{}".format(details, basis)
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_survey(survey, index):
    """Compare one survey and stage its remark. Returns the outcome string.

    Does not commit - the caller commits once for the whole batch.
    """

    match = match_survey(survey, index)

    api_row = match["row"]
    ambiguous = match["ambiguous"]

    if api_row is None:
        shortfalls = None
        outcome = "ambiguous_section" if ambiguous else "no_bucket_data"
        matched = False

    else:

        shortfalls = find_shortfalls(survey, api_row)

        if shortfalls:
            outcome = "shortfall"
            matched = False
        else:
            outcome = "matched"
            matched = True

    survey.roadvision_remark = build_remark(
        survey,
        api_row,
        shortfalls,
        ambiguous,
        missing=match["missing"],
        combined=match["combined"],
    )
    survey.video_count_matched = matched
    survey.video_count_checked_at = datetime.utcnow()

    # Only a clean pass closes the review. Shortfalls and missing bucket data
    # stay open so they remain in the RoadVision queue for someone to action.
    if matched and not survey.roadvision_completed:

        survey.roadvision_completed = True
        survey.roadvision_completed_at = datetime.utcnow()

    return outcome


def select_surveys(days=DEFAULT_WINDOW_DAYS):
    """Completed surveys with uploaded video, inside the rolling window.

    video_upload_time is naive UTC (routes/captain.py). end_time is tz-aware IST,
    so it must not be used here - comparing the two raises.
    """

    cutoff = datetime.utcnow() - timedelta(days=days)

    return Survey.query.filter(
        Survey.status == "completed",
        Survey.video_uploaded == True,
        Survey.video_upload_time != None,
        Survey.video_upload_time >= cutoff,
    ).order_by(
        Survey.id.desc()
    ).all()


def has_manual_remark(survey):
    """True if this remark was written by a person, not by this sync.

    video_count_checked_at is only ever set by sync_survey, so a survey holding a
    remark without it was reviewed by hand and must not be overwritten.
    """

    if survey.video_count_checked_at is not None:
        return False

    return bool((survey.roadvision_remark or "").strip())


def check_surveys(
    survey_ids,
    session=None,
    preserve_manual=True,
    timeout=None,
):
    """Compare specific surveys by id and write their remarks. Commits.

    This is the entry point for the on-completion hook (utils/video_count_hook.py),
    which is why it takes a `session`: that hook runs from an after_commit
    listener, where db.session is between transactions and must not emit SQL.
    Passing a fresh Session also means a failure in here can never roll back the
    completion that triggered it. Defaults to db.session so it stays usable from
    a shell or a route.

    Skips anything that is not actually a completed survey with uploaded video,
    so it applies exactly the same preconditions as select_surveys.
    """

    if session is None:
        session = db.session

    summary = {
        "requested": len(survey_ids),
        "checked": 0,
        "matched": 0,
        "shortfall": 0,
        "no_bucket_data": 0,
        "ambiguous_section": 0,
        "skipped": 0,
    }

    if not survey_ids:
        return summary

    rows = fetch_counts(timeout=timeout)

    if rows is None:
        summary["error"] = "upstream_unavailable"
        return summary

    index = build_index(rows)

    for survey_id in survey_ids:

        survey = session.get(Survey, survey_id)

        if survey is None:
            summary["skipped"] += 1
            continue

        # The hook fires on the status transition, but a survey can reach
        # "completed" by a path that never recorded counts. Nothing to compare.
        if survey.status != "completed" or not survey.video_uploaded:
            summary["skipped"] += 1
            continue

        if preserve_manual and has_manual_remark(survey):
            summary["skipped"] += 1
            continue

        outcome = sync_survey(survey, index)

        summary["checked"] += 1
        summary[outcome] += 1

    session.commit()

    return summary


def sync_recent(
    days=DEFAULT_WINDOW_DAYS,
    dry_run=False,
    force_fetch=False,
    preserve_manual=True,
):
    """Compare every in-window survey and write remarks.

    Idempotent. Surveys already passing are skipped, so a shortfall caused by a
    still-running upload self-heals on a later run once the videos land.

    Hand-written remarks are left untouched unless preserve_manual=False.
    """

    summary = {
        "window_days": days,
        "considered": 0,
        "skipped_already_matched": 0,
        "skipped_manual_remark": 0,
        "checked": 0,
        "matched": 0,
        "shortfall": 0,
        "no_bucket_data": 0,
        "ambiguous_section": 0,
        "dry_run": dry_run,
    }

    expiry = check_token_expiry()

    if expiry:
        summary.update(expiry)

    rows = fetch_counts(force=force_fetch)

    if rows is None:
        summary["error"] = "upstream_unavailable"
        return summary

    index = build_index(rows)

    surveys = select_surveys(days)

    summary["considered"] = len(surveys)

    for survey in surveys:

        if survey.video_count_matched is True:
            summary["skipped_already_matched"] += 1
            continue

        if preserve_manual and has_manual_remark(survey):
            summary["skipped_manual_remark"] += 1
            continue

        outcome = sync_survey(survey, index)

        summary["checked"] += 1
        summary[outcome] += 1

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()

    return summary
