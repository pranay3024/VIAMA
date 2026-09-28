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

# The primary model handles the normal form read. Missing or conflicting dates
# escalate to the stronger model because handwriting and page layout vary.
MODEL_ID = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
DATE_RETRY_MODEL_ID = (
    os.getenv("GEMINI_DATE_MODEL") or "gemini-3.6-flash"
).strip()

_date_cache = {}


# Google can drop connections when several syncs run at once (SSL EOF /
# RemoteDisconnected). Serialising extraction avoids the connection churn that
# triggers those drops.
_EXTRACT_LOCK = threading.Lock()


def _safe_generate(contents, config, model_id=MODEL_ID):
    """Call generate_content with retries on transient transport errors."""
    import httpx
    from http.client import RemoteDisconnected

    last_exc = None
    for attempt in range(2):
        started = time.time()
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config,
            )
            print(
                f"[GEMINI_CALL] ts={datetime.utcnow().isoformat()} "
                f"model={model_id} ok=True attempt={attempt + 1} "
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
                f"model={model_id} ok=False attempt={attempt + 1} "
                f"elapsed={time.time() - started:.1f}s err={exc}",
                flush=True,
            )
            if attempt < 1:
                time.sleep(0.5)
    raise last_exc


class SurveyDates(BaseModel):
    end_date: Optional[str] = None
    end_confidence: Optional[float] = None


class SurveyFormFields(BaseModel):
    start_date: Optional[str] = None
    start_confidence: Optional[float] = None
    end_date: Optional[str] = None
    end_confidence: Optional[float] = None
    ae_ie_sc_name: Optional[str] = None
    piu_name: Optional[str] = None
    contractor_agency: Optional[str] = None


MIN_SURVEY_DATE = date(2026, 6, 1)
MAX_SURVEY_DATE = date.today()

# A field survey runs at most 2-3 days, so the handwritten start and end
# dates on one form are never more than a few days apart. A wider gap means
# one reading is wrong (usually a misread month/day on a blurry form) and is
# used to trigger a focused re-read instead of storing the bad value.
MAX_START_END_GAP_DAYS = 3


def _dates_consistent(start_iso, end_iso):
    """True unless both dates are known and more than the survey window apart."""
    if not start_iso or not end_iso:
        return True
    gap = abs((date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days)
    return gap <= MAX_START_END_GAP_DAYS


def _enhance_header(png_bytes):
    """Contrast-boost and sharpen an enlarged date-header crop.

    Zooming a blurry scan further cannot invent detail the scan never had;
    autocontrast plus an unsharp mask recovers the soft edges of a blurred
    pen stroke, which is what the vision model keys digit boundaries on.
    Best-effort: any failure returns the original bytes untouched.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps

        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        image = ImageOps.autocontrast(image)
        image = image.filter(
            ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] header enhancement skipped: {exc}", flush=True)
        return png_bytes


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
        return full_page.tobytes("png"), _enhance_header(header.tobytes("png"))
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
- Read only the date beside the Survey End Date/To label. Never copy the Survey Start Date as the end date.
- Read every handwritten digit in the Survey End Date row independently; never copy the Survey Start Date.
- The end date may be different from the start date. Do not assume they are equal or consecutive.
- The survey runs at most 2-3 days, so the end date is always within 2-3 days of the start date shown next to it. If the date you read is more than 3 days away from the start date, re-read the end-date digits - you have most likely misread a month or day digit (common on blurry strokes).
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


def _get_pdf_page_images(pdf_bytes, max_pages=8, scale=1.5):
    """Render form pages plus high-resolution views covering page 1."""
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = min(len(document), max_pages)
        images = []
        for page_index in range(pages):
            pix = document[page_index].get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
            )
            images.append(pix.tobytes("png"))
        date_views = []
        if pages:
            page = document[0]
            clips = (
                fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.55),
                fitz.Rect(
                    0,
                    page.rect.height * 0.45,
                    page.rect.width,
                    page.rect.height,
                ),
            )
            for clip in clips:
                date_view = page.get_pixmap(
                    matrix=fitz.Matrix(3, 3),
                    clip=clip,
                    alpha=False,
                ).tobytes("png")
                date_views.append(_enhance_header(date_view))
        return images, date_views
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] Page rendering issue: {exc}", flush=True)
        return [pdf_bytes], [pdf_bytes]


def _parse_gemini_response(response):
    """Return the parsed JSON dict (or None) from a generate_content response."""
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
        text = "".join(response_parts).strip() or (
            getattr(response, "text", "") or ""
        ).strip()
        if not text:
            return None
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        json_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                data = None

    return data if isinstance(data, dict) else None


def _coerce_confidence(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def _clean_text(value):
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value.strip()).strip()
    return cleaned or None


def extract_survey_form_fields_from_pdf(pdf_bytes):
    """Extract all survey-form fields and repair missing or invalid dates."""

    text_end_date = _extract_end_date_from_pdf_text(pdf_bytes)

    images, date_views = _get_pdf_page_images(pdf_bytes)

    prompt = """
Do not output any reasoning, chain of thought, explanations, or preamble. Output ONLY the extracted fields directly in JSON.

Extract these fields from this NHAI road-survey form:
1. start_date - the handwritten Survey Start Date / From date.
2. start_confidence - visual certainty of the start date (0.0 to 1.0).
3. end_date - the handwritten Survey End Date / To date.
4. end_confidence - visual certainty of the end date (0.0 to 1.0).
5. ae_ie_sc_name - the printed name/designation of the AE/IE/SC who checked the form.
6. piu_name - the printed PIU name.
7. contractor_agency - the printed Contractor / O&M Agency name.

Required JSON format:
{
  "start_date": null,
  "start_confidence": 0.0,
  "end_date": null,
  "end_confidence": 0.0,
  "ae_ie_sc_name": null,
  "piu_name": null,
  "contractor_agency": null
}

Date-reading rules:
- The handwritten dates may be in the top header, the middle of the form, or the bottom sign-off area. They are not guaranteed to be in the top 30 percent. Inspect every supplied view of page 1 and locate the printed row labels before reading the writing.
- Survey Start Date / From means start_date. Survey End Date / To means end_date. Never copy one row's date into the other row and never use a date from another page.
- Read every handwritten digit independently. Faint, small, or imperfect handwriting is still a date to transcribe; do not return null merely because the writing is handwritten or low contrast. If a digit is uncertain, return the best visual reading and set that date's confidence below 0.85.
- Use null for a date only when its labelled row has no readable date at all. When a date is null, set its confidence to 0.0.
- The project began in June 2026. Reject a year before 2026. A short year "026" means 2026.
- Accept formats such as 4 September 2026, 04/09/2026, 2026-09-04, 04-09-026, and dates separated by vertical bars. Treat a bar or stroke as a separator, not as the digit 1.
- For numeric dates with an ambiguous day/month order, use day-first DD/MM/YYYY for this Indian form.
- The start and end dates may be equal, but must be within 0-3 days. Use this only as a consistency check; do not change a clearly visible digit to force the rule.

Printed-field rules:
- Copy ae_ie_sc_name, piu_name, and contractor_agency exactly as printed, preserving spelling and case.
- Use null for a printed field that is absent or unreadable.
- Confidence values must be between 0.0 and 1.0 and reflect visual certainty only.
"""

    config = types.GenerateContentConfig(
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=SurveyFormFields,
    )

    if text_end_date:
        prompt = prompt + (
            "\nKnown Survey End Date (verified from the form's text layer, "
            f"authoritative): {text_end_date}\n"
        )

    contents = ["COMPLETE SURVEY FORM PAGE 1:"]
    contents.append(types.Part.from_bytes(data=images[0], mime_type="image/png"))
    for index, date_view in enumerate(date_views, start=1):
        contents.append(f"HIGH-RESOLUTION PAGE 1 VIEW {index}:")
        contents.append(types.Part.from_bytes(data=date_view, mime_type="image/png"))
    for index in range(1, len(images)):
        contents.append(f"ADDITIONAL SURVEY FORM PAGE {index + 1}:")
        contents.append(types.Part.from_bytes(data=images[index], mime_type="image/png"))
    contents.append(prompt)

    response = _safe_generate(contents, config)

    data = _parse_gemini_response(response)
    if data is None:
        raise ValueError("Gemini returned unparseable JSON for the survey form fields")

    normalized = {str(k).lower(): v for k, v in data.items()}

    fields = {
        "start_date": _valid_date(normalized.get("start_date")),
        "start_confidence": _coerce_confidence(normalized.get("start_confidence")),
        "end_date": _valid_date(normalized.get("end_date")),
        "end_confidence": _coerce_confidence(normalized.get("end_confidence")),
        "ae_ie_sc_name": _clean_text(normalized.get("ae_ie_sc_name")),
        "piu_name": _clean_text(normalized.get("piu_name")),
        "contractor_agency": _clean_text(normalized.get("contractor_agency")),
    }

    # The form's printed text layer is the most trustworthy end-date source a
    # scan has - it cannot be misread the way handwriting can. Prefer it over
    # the vision reading whenever it is present.
    if text_end_date:
        fields["end_date"] = text_end_date
        fields["end_confidence"] = 1.0

    needs_date_repair = (
        not fields["start_date"]
        or not fields["end_date"]
        or not _dates_consistent(fields["start_date"], fields["end_date"])
    )
    if needs_date_repair:
        fields = _repair_inconsistent_dates(
            fields, images, date_views, text_end_date, config
        )

    print(f"[GEMINI SURVEY FORM] parsed result: {fields}", flush=True)
    return fields


def _repair_inconsistent_dates(fields, images, date_views, text_end_date, config):
    start = fields.get("start_date")
    end = fields.get("end_date")
    missing_start = not start
    missing_end = not end
    inconsistent = bool(
        start and end and not _dates_consistent(start, end)
    )
    print(
        f"[GEMINI SURVEY FORM] date re-read start={start} end={end} "
        f"missing_start={missing_start} missing_end={missing_end} "
        f"inconsistent={inconsistent}",
        flush=True,
    )

    if text_end_date:
        anchor = (
            f"The verified Survey End Date is {end}. Keep end_date exactly "
            f"as {end} and re-read the Survey Start Date independently."
        )
    elif start and missing_end:
        anchor = (
            f"The first pass read the Survey Start Date as {start}. Keep that "
            "reading unless the pixels clearly contradict it, and re-read the "
            "Survey End Date independently."
        )
    elif end and missing_start:
        anchor = (
            f"The first pass read the Survey End Date as {end}. Keep that "
            "reading unless the pixels clearly contradict it, and re-read the "
            "Survey Start Date independently."
        )
    else:
        anchor = (
            "Re-read both handwritten dates independently from their printed "
            "row labels."
        )

    prompt = f"""
Return only JSON with these keys: start_date, start_confidence, end_date, end_confidence.

This is a focused re-read of an NHAI road-survey form. The date rows may be
anywhere on page 1, including the bottom sign-off area. Locate the printed
labels first: Survey Start Date / From is start_date and Survey End Date / To
is end_date. The previous pass returned start={start} and end={end}.

{anchor}

Read the actual handwriting in the high-resolution views. Faint or imperfect
handwriting is still a date; return the best visual transcription and a
confidence below 0.85 if a digit is uncertain. Do not return null merely
because the writing is handwritten, small, or low contrast. Use null only when
the labelled row has no readable date. Read the day, month, and year digits
independently. Use DD/MM/YYYY for numeric dates, treat 026 as 2026, and treat
vertical bars or strokes as separators rather than digits. The two dates must
be within {MAX_START_END_GAP_DAYS} days, but do not alter a visible digit just
to satisfy that check.
"""

    retry_config = types.GenerateContentConfig(
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=SurveyFormFields,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    contents = ["FOCUSED FULL-PAGE DATE RE-READ:"]
    for index, date_view in enumerate(date_views, start=1):
        contents.append(f"HIGH-RESOLUTION PAGE 1 VIEW {index}:")
        contents.append(types.Part.from_bytes(data=date_view, mime_type="image/png"))
    contents.extend(
        [
            "COMPLETE PAGE 1 FOR LABEL CONTEXT:",
            types.Part.from_bytes(data=images[0], mime_type="image/png"),
            prompt,
        ]
    )

    try:
        retry_response = _safe_generate(
            contents,
            retry_config,
            model_id=DATE_RETRY_MODEL_ID,
        )
    except Exception as exc:
        print(
            f"[GEMINI SURVEY FORM] stronger date model failed: {exc}; "
            "using the primary model",
            flush=True,
        )
        retry_response = _safe_generate(contents, config)

    retry_data = _parse_gemini_response(retry_response) or {}
    corrected = {
        "start_date": _valid_date(retry_data.get("start_date")),
        "start_confidence": _coerce_confidence(
            retry_data.get("start_confidence")
        ),
        "end_date": _valid_date(retry_data.get("end_date")),
        "end_confidence": _coerce_confidence(
            retry_data.get("end_confidence")
        ),
    }
    print(f"[GEMINI SURVEY FORM] re-read result: {corrected}", flush=True)

    if missing_start and corrected["start_date"]:
        fields["start_date"] = corrected["start_date"]
        fields["start_confidence"] = corrected["start_confidence"]
    if missing_end and corrected["end_date"] and not text_end_date:
        fields["end_date"] = corrected["end_date"]
        fields["end_confidence"] = corrected["end_confidence"]
    if inconsistent:
        if corrected["start_date"]:
            fields["start_date"] = corrected["start_date"]
            fields["start_confidence"] = corrected["start_confidence"]
        if corrected["end_date"] and not text_end_date:
            fields["end_date"] = corrected["end_date"]
            fields["end_confidence"] = corrected["end_confidence"]

    if text_end_date:
        fields["end_date"] = text_end_date
        fields["end_confidence"] = 1.0

    if not fields["start_date"]:
        fields["start_confidence"] = None
    if not fields["end_date"]:
        fields["end_confidence"] = None

    if _dates_consistent(fields["start_date"], fields["end_date"]):
        return fields

    print(
        "[GEMINI SURVEY FORM] date pair still inconsistent after re-read - "
        "dropping the lower-confidence field",
        flush=True,
    )
    if text_end_date or (start and missing_end):
        fields["end_date"] = None
        fields["end_confidence"] = None
    elif end and missing_start:
        fields["start_date"] = None
        fields["start_confidence"] = None
    elif (fields["start_confidence"] or 0) > (fields["end_confidence"] or 0):
        fields["end_date"] = None
        fields["end_confidence"] = None
    else:
        fields["start_date"] = None
        fields["start_confidence"] = None
    return fields


def _has_complete_survey_dates(fields):
    return bool(
        fields.get("start_date")
        and fields.get("end_date")
        and _dates_consistent(fields["start_date"], fields["end_date"])
    )


def extract_survey_form_fields_from_drive(view_url):
    """Download a survey form from Drive and extract all fields."""
    cached = _form_fields_cache.get(view_url)
    if cached is not None and _has_complete_survey_dates(cached):
        print("[GEMINI SURVEY FORM] using cached validated form fields", flush=True)
        return cached
    if view_url in _form_fields_cache:
        _form_fields_cache.pop(view_url, None)

    with _EXTRACT_LOCK:
        cached = _form_fields_cache.get(view_url)
        if cached is not None and _has_complete_survey_dates(cached):
            print(
                "[GEMINI SURVEY FORM] using cached validated form fields",
                flush=True,
            )
            return cached
        print("[GEMINI SURVEY FORM] downloading survey form from Drive", flush=True)
        pdf_bytes = download_file_from_drive(view_url)
        print(
            f"[GEMINI SURVEY FORM] downloaded PDF bytes: {len(pdf_bytes)}",
            flush=True,
        )

        fields = extract_survey_form_fields_from_pdf(pdf_bytes)
        if _has_complete_survey_dates(fields):
            _form_fields_cache[view_url] = fields
        else:
            print(
                "[GEMINI SURVEY FORM] incomplete result not cached; a later "
                "attempt will re-read the PDF",
                flush=True,
            )
        return fields


def apply_survey_form_fields(survey, fields):
    """Assign extracted form fields onto a Survey object without committing."""
    manual = getattr(survey, "defect_report_match_status", None) == "manual"
    if not manual and fields.get("start_date"):
        survey.extracted_survey_start_date = date.fromisoformat(
            fields["start_date"]
        )
        survey.survey_start_date_confidence = fields.get("start_confidence")
    if not manual and fields.get("end_date"):
        survey.extracted_survey_end_date = date.fromisoformat(fields["end_date"])
        survey.survey_end_date_confidence = fields.get("end_confidence")
    for field in (
        "ae_ie_sc_name",
        "piu_name",
        "contractor_agency",
    ):
        value = fields.get(field)
        if value is not None:
            setattr(survey, f"extracted_{field}", value)


_form_fields_cache = {}

