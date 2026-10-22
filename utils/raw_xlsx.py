"""Download the RoadVision raw-video spreadsheet for a survey stretch."""

import os
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://gis.roadvision.ai/api/raw-xlsx"
LIST_API_URL = f"{API_URL}/list"
TIMEOUT_SECONDS = 20
LIST_TIMEOUT_SECONDS = 5


class RawXlsxUnavailable(RuntimeError):
    """The matching raw XLSX is not ready or the upstream is temporarily down."""


def normalise_dn(value):
    """Day/night marker as the portal means it: ``"night"`` or ``""`` for day.

    RoadVision is not consistent about this field - the raw-XLSX inventory
    leaves it empty for a day survey while the defect-report inventory spells
    out ``"day"``.  Both mean the same thing, so both are accepted.
    """
    text = str(value or "").strip().casefold()
    if text in ("night", "n", "night_shift"):
        return "night"
    return ""


def raw_xlsx_item_matches(item, section_no, upc_code, cycle_no, survey_type):
    """Whether one inventory entry is this survey's stretch, cycle and shift."""
    if not isinstance(item, dict) or cycle_no is None:
        return False

    labels = set()
    for value in (section_no, upc_code):
        for part in re.split(r"\s*&\s*", str(value or "")):
            label = re.sub(r"[^A-Za-z0-9]", "", part).upper()
            if label:
                labels.add(label)

    if not labels:
        return False

    label = re.sub(r"[^A-Za-z0-9]", "", str(item.get("label") or "")).upper()
    try:
        item_cycle = int(item.get("cycle"))
    except (TypeError, ValueError):
        return False

    expected_dn = "night" if str(survey_type or "").strip().casefold() == "night" else ""
    item_dn = normalise_dn(item.get("dn"))

    return label in labels and item_cycle == int(cycle_no) and item_dn == expected_dn


def raw_xlsx_is_listed(section_no, upc_code, cycle_no, survey_type, files):
    """Whether the inventory contains this survey's stretch, cycle and day/night."""
    if cycle_no is None:
        return False

    for item in files or []:
        if raw_xlsx_item_matches(item, section_no, upc_code, cycle_no, survey_type):
            return True

    return False


def list_raw_xlsx_files():
    """Return the RoadVision raw-XLSX inventory metadata."""
    api_key = (os.getenv("RV_RAW_XLSX_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("RV_RAW_XLSX_API_KEY is not configured")

    request = Request(
        LIST_API_URL,
        headers={"X-API-Key": api_key, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=LIST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawXlsxUnavailable("RoadVision raw XLSX inventory is unavailable") from exc

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise RawXlsxUnavailable("RoadVision returned an invalid raw XLSX inventory")
    return files


def download_raw_xlsx(section_no, cycle_no, survey_type):
    """Return ``(xlsx_bytes, filename)`` for the survey's stretch and cycle."""
    api_key = (os.getenv("RV_RAW_XLSX_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("RV_RAW_XLSX_API_KEY is not configured")

    stretch = str(section_no or "").strip()
    if not stretch or cycle_no is None:
        raise ValueError("A stretch number and cycle are required for the raw XLSX")

    is_night = str(survey_type or "").strip().casefold() == "night"
    params = {"stretch": stretch, "cycle": str(cycle_no)}
    if is_night:
        params["dn"] = "night"

    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"X-API-Key": api_key},
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            content = response.read()
    except HTTPError as exc:
        if exc.code not in (404, 502, 503, 504):
            raise
        raise RawXlsxUnavailable(
            f"RoadVision raw XLSX download failed for stretch {stretch}, "
            f"cycle {cycle_no}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise RawXlsxUnavailable(
            f"RoadVision raw XLSX download failed for stretch {stretch}, "
            f"cycle {cycle_no}"
        ) from exc

    if not content.startswith(b"PK"):
        raise RawXlsxUnavailable(
            f"RoadVision returned an invalid raw XLSX for stretch {stretch}, "
            f"cycle {cycle_no}"
        )

    safe_stretch = re.sub(r"[^A-Za-z0-9_-]+", "_", stretch).strip("_")
    night_suffix = "_Night" if is_night else ""
    filename = f"Raw_Video_{safe_stretch}_Cycle{cycle_no}{night_suffix}.xlsx"
    return content, filename