import io
import json
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

import fitz
import pikepdf
from google import genai
from google.genai import types
from pydantic import BaseModel

from google_drive import download_file_from_drive


from dotenv import load_dotenv

load_dotenv()

# Validate API key
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not set in the environment.")

client = genai.Client(api_key=api_key)

# Cheap flash-lite model: date extraction is a low-complexity vision task, so
# the full flash tier (gemini-3.6-flash) is overkill and costs ~2.5x more on
# input. Override any time via GEMINI_MODEL if accuracy demands it.
MODEL_ID = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()

_date_cache = {}


# Google can drop connections when several syncs run at once (SSL EOF /
# RemoteDisconnected). Serialising extraction avoids the connection churn that
# triggers those drops.
_EXTRACT_LOCK = threading.Lock()


def _safe_generate(contents, config):
    """Call generate_content with retries on transient transport errors."""
    import httpx
    from http.client import RemoteDisconnected

    last_exc = None
    for attempt in range(2):
        started = time.time()
        try:
            response = client.models.generate_content(
                model=MODEL_ID,
                contents=contents,
                config=config,
            )
            print(
                f"[GEMINI_CALL] ts={datetime.utcnow().isoformat()} "
                f"model={MODEL_ID} ok=True attempt={attempt + 1} "
                f"elapsed={time.time() - started:.1f}s",
                flush=True,
            )
            return response
        except (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            ConnectionError,
            RemoteDisconnected,
        ) as exc:
            last_exc = exc
            print(
                f"[GEMINI_CALL] ts={datetime.utcnow().isoformat()} "
                f"model={MODEL_ID} ok=False attempt={attempt + 1} "
                f"elapsed={time.time() - started:.1f}s err={exc}",
                flush=True,
            )
            if attempt < 1:
                time.sleep(0.5)
    raise last_exc


class SurveyDates(BaseModel):
    end_date: Optional[str] = None
    end_confidence: Optional[float] = None


MIN_SURVEY_DATE = date(2026, 6, 1)
MAX_SURVEY_DATE = date.today()


def _valid_date(value):
    if not isinstance(value, str):
        return None

    value = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"[,]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    normalized = value.replace("|", "-").replace("/", "-").replace(".", "-")
    # Collapse spaces around separators so e.g. "31 - 08 - 026" parses. Multi-word
    # month names ("4 September 2026") are unaffected (they have no dashes).
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().strip("-")
    # Short 3-digit years on this survey form are abbreviated e.g. "026" = 2026.
    # Expand "31-08-026" -> "31-08-2026". The leading zero is consumed so the
    # captured group is just the two-digit year.
    normalized = re.sub(r"-0(\d{2})$", r"-20\1", normalized)
    date_formats = (
        "%d-%m-%Y",
        "%d-%m-%y",
        "%d-%B-%Y",
        "%d-%b-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%Y-%m-%d",
    )
    for date_format in date_formats:
        try:
            parsed = datetime.strptime(normalized, date_format).date()
            if parsed < MIN_SURVEY_DATE or parsed > MAX_SURVEY_DATE:
                return None
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _get_first_page_images(pdf_bytes):
    """Render page 1 and an enlarged crop of its date header."""
    try:
        source_pdf = pikepdf.Pdf.open(io.BytesIO(pdf_bytes))
        if len(source_pdf.pages) > 1:
            first_page_pdf = pikepdf.Pdf.new()
            first_page_pdf.pages.append(source_pdf.pages[0])
            output = io.BytesIO()
            first_page_pdf.save(output)
            pdf_bytes = output.getvalue()

        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = document[0]
        # Token minimisation: Gemini bills images per 768px tile. A 4x full A4
        # page (~2380x3368px) is ~20 tiles (~5k tokens) - the single biggest
        # cost of one extraction. Render the full page at 2x (still legible)
        # and keep only the date header crop at 3x where the handwriting is.
        full_page = page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            alpha=False,
        )
        header_clip = fitz.Rect(
            0,
            0,
            page.rect.width,
            page.rect.height * 0.30,
        )
        header = page.get_pixmap(
            matrix=fitz.Matrix(3, 3),
            clip=header_clip,
            alpha=False,
        )
        return full_page.tobytes("png"), header.tobytes("png")
    except Exception as exc:
        print(f"[GEMINI SURVEY DATES] Page rendering issue: {exc}", flush=True)
        return pdf_bytes, pdf_bytes


def _extract_end_date_from_pdf_text(pdf_bytes):
    """Extract an end date from a text-backed PDF before using Gemini."""
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = document[0].get_text("text")
    except Exception:
        return None

    label_match = re.search(
        r"survey\s*end\s*date|end\s*date",
        text,
        flags=re.IGNORECASE,
    )
    if not label_match:
        return None

    nearby_text = text[label_match.end():label_match.end() + 120]
    candidates = re.findall(
        r"\b\d{1,2}\s*[/|.-]\s*\d{1,2}\s*[/|.-]\s*\d{2,4}\b"
        r"|\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
        r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
        r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{4}\b",
        nearby_text,
        flags=re.IGNORECASE,
    )
    for candidate in candidates:
        normalized = _valid_date(candidate)
        if normalized:
            return normalized
    return None


def extract_survey_dates_from_pdf(pdf_bytes):
    text_date = _extract_end_date_from_pdf_text(pdf_bytes)
    if text_date:
        dates = {"end_date": text_date, "end_confidence": 1.0}
        print(
            f"[GEMINI SURVEY DATES] parsed text-layer result: {dates}",
            flush=True,
        )
        return dates

    full_page_bytes, header_bytes = _get_first_page_images(pdf_bytes)

    prompt = """
Do not output any reasoning, chain of thought, explanations, or preamble. Output ONLY the extracted dates directly in JSON.

Extract only the survey end date from page 1 of this road survey form.
Two views are supplied: the complete page and an enlarged crop of the top header.
The top header is usually where the handwritten dates appear.

Required JSON format:
{
    "end_date": "extracted end date",
    "end_confidence": 0.0
}

Rules:
- Read only the date beside the Survey End Date/To label. Ignore Survey Start Date completely.
- Read every handwritten digit in the Survey End Date row independently; never copy the Survey Start Date.
- The end date may be different from the start date. Do not assume they are equal or consecutive.
- If any end-date digit is ambiguous, return the end date with confidence below 0.85 so the record is flagged for review rather than silently guessed.
- Inspect the enlarged header crop carefully, including handwritten digits.
- A vertical separator stroke or bar before a date is not the digit 1. For example, read `| 3/08/2026` as `03/08/2026`, never `31/08/2026`.
- The survey form may also write the date split by vertical bars, e.g. `31 | 08 | 026` or `03 | 08 | 2026`. Treat each `|` as a plain separator between day, month and year, never as a digit.
- A short 3-digit year like `026` means 2026 (the project year). Normalize it to 2026.
- Accept any clearly printed date format (e.g., 4 September 2026, 04/09/2026, 2026-09-04).
- For numeric dates with an ambiguous day/month order, use day-first order (DD/MM/YYYY) for this Indian survey form.
- Do not guess, repair, or infer unclear digits. Return null for a date that cannot be read confidently.
- Return null for missing, blurry, or uncertain dates.
- The survey project began in June 2026. Reject any year before 2026.
- Never output 2020, 2021, 2022, 2023, 2024, or 2025 for this project.
- Confidence must be between 0.0 and 1.0 and reflect visual certainty only.
"""

    # Note: gemini-3.6-flash enforces strict schema & JSON output via response_mime_type & response_schema.
    # Custom sampling settings (temperature, top_p, thinking_budget) are omitted to avoid parameter rejection.
    config = types.GenerateContentConfig(
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=SurveyDates,
    )

    response = _safe_generate(
        [
            "COMPLETE FIRST PAGE:",
            types.Part.from_bytes(data=full_page_bytes, mime_type="image/png"),
            "ENLARGED DATE HEADER:",
            types.Part.from_bytes(data=header_bytes, mime_type="image/png"),
            prompt,
        ],
        config,
    )

    data = None
    parsed_response = getattr(response, "parsed", None)
    if isinstance(parsed_response, dict):
        data = parsed_response
    elif parsed_response is not None:
        if hasattr(parsed_response, "model_dump"):
            data = parsed_response.model_dump()
        elif hasattr(parsed_response, "dict"):
            data = parsed_response.dict()

    if data is None:
        response_parts = []
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                part_text = getattr(part, "text", None)
                if part_text:
                    response_parts.append(part_text)

        text = "".join(response_parts).strip() or (getattr(response, "text", "") or "").strip()

        if not text:
            raise ValueError("Gemini returned an empty response")

        print(f"[GEMINI RAW RESPONSE]: {text}", flush=True)
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        json_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                data = None

        if data is None:
            raise ValueError(f"Gemini returned unparseable JSON: {text}")

    normalized_data = {str(k).lower(): v for k, v in data.items()} if isinstance(data, dict) else {}

    dates = {
        "end_date": _valid_date(normalized_data.get("end_date")),
        "end_confidence": float(normalized_data.get("end_confidence") or 0),
    }

    if not dates["end_date"]:
        retry_prompt = """
Return only JSON for the survey end date visible in the supplied images.
Look specifically at the row labelled Survey End Date or To. Do not use the
Survey Start Date. Read the date exactly as written, including day, month and
year. Use DD/MM/YYYY for numeric dates. Return null only if the end-date row is
not readable.
"""
        retry_response = _safe_generate(
            [
                "FOCUSED END-DATE RETRY:",
                types.Part.from_bytes(data=header_bytes, mime_type="image/png"),
                types.Part.from_bytes(data=full_page_bytes, mime_type="image/png"),
                retry_prompt,
            ],
            config,
        )
        retry_data = getattr(retry_response, "parsed", None)
        if retry_data is not None:
            if hasattr(retry_data, "model_dump"):
                retry_data = retry_data.model_dump()
            elif hasattr(retry_data, "dict"):
                retry_data = retry_data.dict()
        if not isinstance(retry_data, dict):
            retry_text = (getattr(retry_response, "text", "") or "").strip()
            retry_match = re.search(r"\{.*\}", retry_text, flags=re.DOTALL)
            retry_data = (
                json.loads(retry_match.group(0))
                if retry_match
                else {}
            )
        dates = {
            "end_date": _valid_date(retry_data.get("end_date")),
            "end_confidence": float(retry_data.get("end_confidence") or 0),
        }

    if not dates["end_date"]:
        raise ValueError(f"Gemini did not return a valid end date. Extracted: {dates}")

    if dates["end_confidence"] < 0.5:
        raise ValueError(f"Gemini date confidence is too low: {dates}")

    print(f"[GEMINI SURVEY DATES] parsed result: {dates}", flush=True)
    return dates


def extract_survey_dates_from_drive(view_url):
    if view_url in _date_cache:
        print("[GEMINI SURVEY DATES] using cached validated dates", flush=True)
        return _date_cache[view_url]

    # Serialise the download + model call so burst-triggered background syncs
    # do not hammer Drive and Gemini simultaneously (which drops connections).
    with _EXTRACT_LOCK:
        print("[GEMINI SURVEY DATES] downloading survey form from Drive", flush=True)
        pdf_bytes = download_file_from_drive(view_url)
        print(f"[GEMINI SURVEY DATES] downloaded PDF bytes: {len(pdf_bytes)}", flush=True)

        dates = extract_survey_dates_from_pdf(pdf_bytes)
        _date_cache[view_url] = dates
        return dates

