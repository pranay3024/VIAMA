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
        raise RuntimeError("RoadVision raw XLSX inventory is unavailable") from exc

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise RuntimeError("RoadVision returned an invalid raw XLSX inventory")
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
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"RoadVision raw XLSX download failed for stretch {stretch}, "
            f"cycle {cycle_no}"
        ) from exc

    if not content.startswith(b"PK"):
        raise RuntimeError(
            f"RoadVision returned an invalid raw XLSX for stretch {stretch}, "
            f"cycle {cycle_no}"
        )

    safe_stretch = re.sub(r"[^A-Za-z0-9_-]+", "_", stretch).strip("_")
    night_suffix = "_Night" if is_night else ""
    filename = f"Raw_Video_{safe_stretch}_Cycle{cycle_no}{night_suffix}.xlsx"
    return content, filename