"""Download the RoadVision raw-video spreadsheet for a survey stretch."""

import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://gis.roadvision.ai/api/raw-xlsx"
TIMEOUT_SECONDS = 20


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