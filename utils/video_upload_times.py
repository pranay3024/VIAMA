"""Bucket video upload times, from the RoadVision GIS API.

The admin delayed-defect report shows when the survey's videos actually landed in
the GCS bucket. That is authoritative in a way our own ``Survey.video_upload_time``
is not: ours is written by the captain's phone and can be missed or entered late,
while the bucket listing is ground truth.

Upstream contract
-----------------
``GET /videos-uploaded-time/all`` returns
``{"result": {"rows": [{"stretch_number", "cycle_number", "first_video_uploaded",
"last_video_uploaded", ...}], "snapshot": {...}, "totals": {...}}}``.

Each of ``first_video_uploaded`` / ``last_video_uploaded`` is null when the bucket
holds nothing for that stretch-cycle, else an object carrying
``uploaded_at_ist`` ("2026-08-12 12:31:03 IST") and ``uploaded_at_ist_iso``.

All times returned by this module are **naive IST**, matching how the delayed
report renders the rest of its timestamps. Do not add an offset to them.

Matching
--------
The API keys rows by ``stretch_number``, which is either a zero-padded section
number ("084") or a UPC-style code ("N_04018_04005_OR"). Our ``section_no`` uses
all of: padded numbers, bare numbers, "84(A)", and "N/04018/04005/OR". So a
survey can match under several spellings; we try all of them and stop at the
first hit. ``survey_match_report`` shows what resolved, for the admin's benefit.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

API_URL = os.getenv(
    "RV_GIS_UPLOAD_TIMES_URL",
    "https://gis.roadvision.ai/videos-uploaded-time/all",
)

# Upstream keeps its own snapshot for 900s and rebuilding costs ~35s, so polling
# faster than this just burns a request to be told the same thing.
CACHE_TTL_SECONDS = 900
FAILURE_RETRY_SECONDS = 35

# A cold fetch rebuilds the upstream snapshot and takes ~35s, so this must clear
# that comfortably or every cache expiry turns into a blank column. The wait is
# bounded and paid at most once per CACHE_TTL_SECONDS; if the GIS API is genuinely
# down we give up rather than hang the admin page.
# Reduced to fail fast on the admin page when the API is unreachable.
FETCH_TIMEOUT_SECONDS = 10

_IST_SUFFIX = " IST"

_cache = {"fetched_at": 0.0, "index": None, "failed_at": 0.0}


# ---------------------------------------------------------------------------
# Upstream fetch
# ---------------------------------------------------------------------------

def _parse_ist(value):
    """'2026-08-12 12:31:03 IST' -> naive datetime. None if unparseable."""
    if not value:
        return None

    text = str(value).strip()

    if text.endswith(_IST_SUFFIX):
        text = text[:-len(_IST_SUFFIX)].strip()

    # The API emits a space where an ISO string wants a 'T'.
    text = text.replace("T", " ").replace("+05:30", "").strip()

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    log.warning("could not parse IST timestamp %r from the GIS API", value)
    return None


def build_index(rows):
    """{(stretch_number, cycle_number): last_video_uploaded as naive IST}."""
    index = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        stretch = (row.get("stretch_number") or "").strip()
        cycle = row.get("cycle_number")

        if not stretch or cycle is None:
            continue

        uploaded = row.get("last_video_uploaded") or {}
        when = _parse_ist(uploaded.get("uploaded_at_ist"))

        # A row with no last video tells us nothing; leave the key absent so the
        # caller reports "no row" rather than a misleading time.
        if when is None:
            continue

        index[(stretch, int(cycle))] = when

    return index


def fetch_index(force=False, timeout=None):
    """Return the {(stretch, cycle): datetime} index, or None if unobtainable.

    Caches for CACHE_TTL_SECONDS and serves the last good index on failure, so a
    brief GIS outage degrades to slightly stale times rather than blank ones.
    """
    token = os.getenv("RV_GIS_TOKEN") or os.getenv("RV_DASHBOARD_TOKEN")

    if not token:
        log.error("RV_GIS_TOKEN/RV_DASHBOARD_TOKEN not set - bucket video upload times unavailable")
        return None

    now = time.time()
    # Let the upstream cold snapshot finish, then retry instead of hiding it
    # behind a long cooldown.
    if _cache["index"] is None and not force:
        if (
            _cache["failed_at"]
            and (now - _cache["failed_at"]) < FAILURE_RETRY_SECONDS
        ):
            return None
    if _cache["index"] is not None and not force:
        if (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
            return _cache["index"]

    api_url = os.getenv("RV_GIS_UPLOAD_TIMES_URL") or API_URL
    request = urllib.request.Request(
        api_url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
    )

    try:
        effective_timeout = timeout if timeout is not None else FETCH_TIMEOUT_SECONDS
        with urllib.request.urlopen(
            request, timeout=effective_timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        index = build_index(payload.get("result", {}).get("rows", []))

        _cache["index"] = index
        _cache["fetched_at"] = time.time()

        log.info("GIS upload times: %s stretch-cycle row(s) with videos", len(index))

        return index

    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()[:200].decode(errors="replace")
        except Exception:
            body = ""
        log.error(
            "GIS upload times upstream %s: %s",
            exc.code,
            body,
        )
    except Exception as exc:
        log.error("GIS upload times fetch failed: %s: %s", type(exc).__name__, exc)

    _cache["failed_at"] = time.time()
    if _cache["index"] is not None:
        log.warning("serving stale GIS upload times from cache")
        return _cache["index"]

    return None


# ---------------------------------------------------------------------------
# Matching a Survey to an API row
# ---------------------------------------------------------------------------

def _spellings(value):
    """Keys for one identifier string, read every way the API might spell it."""
    keys = set()

    value = (value or "").strip()

    if not value:
        return keys

    if value.isdigit():
        keys.add(value.zfill(3))
        keys.add(value)
        return keys

    # "84(A)" -> "84A"
    packed = re.sub(r"[^A-Za-z0-9]", "", value).upper()

    if packed:
        keys.add(packed)

        if packed.isdigit():
            keys.add(packed.zfill(3))

    # "N/04018/04005/OR" -> "N_04018_04005_OR"
    if "/" in value:
        keys.add(value.replace("/", "_").upper())

    return {key for key in keys if key}


def candidate_stretch_keys(section_no, upc_code=None):
    """Every spelling under which this survey might appear in the API.

    ``section_no`` is not a dependable key on its own: it is sometimes the
    zero-padded section number ("084"), sometimes bare ("84"), sometimes carries
    a package suffix ("84(A)"), sometimes *is* a UPC code
    ("N/04018/04005/OR"), and sometimes covers several stretches at once
    ("1 & 2", "116 & N/08041/01002/UP"). So we split on "&", and try both
    ``section_no`` and the authoritative ``upc_code`` under every spelling.
    """
    keys = set()

    for source in (section_no, upc_code):
        # Split only on "&": it is the one separator that joins two distinct
        # stretches. A "/" is part of a UPC code, not a separator.
        for part in re.split(r"\s*&\s*", str(source or "")):
            keys |= _spellings(part)

    return keys


def lookup_matches(section_no, cycle_no, upc_code=None, index=None):
    """All (stretch_key, last_video_uploaded) pairs that resolve for this survey.

    Usually one pair. A compound ``section_no`` like "1 & 2" genuinely matches
    two stretches in the same cycle, so callers must handle several.
    """
    if index is None:
        index = fetch_index()

    if not index or cycle_no is None:
        return []

    matches = []

    for key in sorted(candidate_stretch_keys(section_no, upc_code)):
        found = index.get((key, int(cycle_no)))
        if found is not None:
            matches.append((key, found))

    return matches


def lookup(section_no, cycle_no, upc_code=None, index=None):
    """Last bucket video upload time for this survey, as naive IST or None.

    When a survey spans several stretches ("1 & 2"), the later time wins: the
    survey is not finished until the last of those stretches' videos landed.
    """
    matches = lookup_matches(section_no, cycle_no, upc_code, index=index)

    if not matches:
        return None

    return max(when for _, when in matches)


def survey_match_report(surveys, timeout=None):
    """Attach a bucket time to each survey in place.

    Sets ``survey.bucket_video_upload_time`` (naive IST, or None) and
    ``survey.bucket_match_key`` (the stretch key that resolved, or None). One
    fetch covers the whole page.

    Returns ``(matched, missing, available)``.
    """
    try:
        index = fetch_index(timeout=timeout)
    except Exception:
        index = None
    available = bool(index)

    matched = 0
    missing = 0

    for survey in surveys:
        matches = lookup_matches(
            survey.section_no,
            survey.cycle_no,
            survey.upc_code,
            index=index,
        )

        # Compound sections resolve to several stretches; the latest time is when
        # the survey as a whole finished uploading.
        when = max((found for _, found in matches), default=None)

        survey.bucket_video_upload_time = when
        survey.bucket_match_key = None
        survey.bucket_match_keys = []

        if when is not None:
            matched += 1

            # Label with the stretch the displayed time actually came from, so the
            # admin tooltip can never contradict the number beside it.
            survey.bucket_match_keys = [key for key, _ in matches]
            survey.bucket_match_key = max(matches, key=lambda pair: pair[1])[0]
        else:
            missing += 1

    if available:
        log.info(
            "bucket video upload times: %s matched, %s without a bucket row",
            matched, missing,
        )
    else:
        log.warning("bucket video upload times unavailable; column will be blank")

    return matched, missing, available