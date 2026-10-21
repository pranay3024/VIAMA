"""Inventory and download RoadVision defect-report spreadsheets."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://gis.roadvision.ai/api/defect-report"
LIST_API_URL = f"{API_URL}/list"
TIMEOUT_SECONDS = 20
LIST_TIMEOUT_SECONDS = 5


class DefectReportUnavailable(RuntimeError):
    """The defect report is not ready or the upstream API is temporarily down."""


def _api_key():
    return (
        os.getenv("RV_DEFECT_REPORT_API_KEY")
        or os.getenv("RV_RAW_XLSX_API_KEY")
        or ""
    ).strip()


def list_defect_report_files():
    """Return the available defect-report inventory metadata."""
    api_key = _api_key()
    if not api_key:
        raise DefectReportUnavailable("RoadVision defect-report API key is not configured")

    request = Request(
        LIST_API_URL,
        headers={"X-API-Key": api_key, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=LIST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DefectReportUnavailable("RoadVision defect-report inventory is unavailable") from exc

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise DefectReportUnavailable("RoadVision returned an invalid defect-report inventory")
    return files


def find_defect_report_file(section_no, upc_code, cycle_no, survey_type, files):
    """Find an inventory entry matching section/UPC, cycle and day/night."""
    if cycle_no is None:
        return None

    identifiers = {
        re.sub(r"[^A-Za-z0-9]", "", str(value or "")).casefold()
        for value in (section_no, upc_code)
    }
    identifiers.discard("")
    if not identifiers:
        return None

    is_night = str(survey_type or "").strip().casefold() == "night"
    for item in files or []:
        if not isinstance(item, dict):
            continue
        try:
            item_cycle = int(item.get("cycle"))
        except (TypeError, ValueError):
            continue
        if item_cycle != int(cycle_no):
            continue

        item_dn = str(item.get("dn") or "").strip().casefold()
        if (item_dn == "night") != is_night:
            continue

        label = str(item.get("label") or "")
        normalized_label = re.sub(r"[^A-Za-z0-9]", "", label).casefold()
        if normalized_label in identifiers:
            return item

    return None


def download_defect_report(stretch, cycle_no, survey_type):
    """Return ``(xlsx_bytes, filename)`` for the matched defect report."""
    api_key = _api_key()
    if not api_key:
        raise DefectReportUnavailable("RoadVision defect-report API key is not configured")

    stretch = str(stretch or "").strip()
    if not stretch or cycle_no is None:
        raise ValueError("A stretch label and cycle are required for the defect report")

    day_night = "night" if str(survey_type or "").strip().casefold() == "night" else "day"
    params = {"stretch": stretch, "cycle": str(cycle_no), "dn": day_night}
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
        raise DefectReportUnavailable(
            f"RoadVision defect report is unavailable for stretch {stretch}, cycle {cycle_no}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise DefectReportUnavailable(
            f"RoadVision defect report is unavailable for stretch {stretch}, cycle {cycle_no}"
        ) from exc

    if not content.startswith(b"PK"):
        raise DefectReportUnavailable(
            f"RoadVision returned an invalid defect report for stretch {stretch}, cycle {cycle_no}"
        )

    safe_stretch = re.sub(r"[^A-Za-z0-9_-]+", "_", stretch).strip("_")
    filename = f"Defect_Report_{safe_stretch}_Cycle{cycle_no}_{day_night.title()}.xlsx"
    return content, filename