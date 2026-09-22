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
    """Render up to ``max_pages`` PNG pages plus an enlarged page-1 date header.

    Called once per survey form so one Gemini request can read every field
    (handwritten dates in the header plus the printed project details).
    """
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
        header = None
        if pages:
            page = document[0]
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
            ).tobytes("png")
            header = _enhance_header(header)
        return images, header
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] Page rendering issue: {exc}", flush=True)
        return [pdf_bytes], pdf_bytes


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
    """Extract ALL survey-form fields in a SINGLE Gemini call.

    Returns a dict with keys: start_date, start_confidence, end_date,
    end_confidence, ae_ie_sc_name, piu_name, contractor_agency. Only dates are
    handwritten; the rest are printed. In the normal case this costs exactly
    one Gemini request. A second, focused re-read is made ONLY when the start
    and end dates contradict the survey window (more than
    MAX_START_END_GAP_DAYS apart) - the main cause is a misread month on a
    blurry form - so the budget stays predictable and wrong dates are never
    stored silently.
    """
    text_end_date = _extract_end_date_from_pdf_text(pdf_bytes)

    images, header_bytes = _get_pdf_page_images(pdf_bytes)

    prompt = """
Do not output any reasoning, chain of thought, explanations, or preamble. Output ONLY the extracted fields directly in JSON.

Extract from this NHAI road-survey form the following fields:
1. start_date - the handwritten Survey Start Date / From date (usually in the top header).
2. start_confidence - visual certainty of the start date (0.0 to 1.0).
3. end_date - the handwritten Survey End Date / To date (usually in the top header).
4. end_confidence - visual certainty of the end date (0.0 to 1.0).
5. ae_ie_sc_name - the printed name/designation of the AE/IE/SC (Assistant Engineer / In-charge Engineer / Sectional Engineer / Engineer-in-Charge) who checked the form.
6. piu_name - the printed PIU (Project Implementation Unit) name.
7. contractor_agency - the printed Contractor / O&M Agency name.

Required JSON format:
{
  "start_date": "",
  "start_confidence": 0.0,
  "end_date": "",
  "end_confidence": 0.0,
  "ae_ie_sc_name": "",
  "piu_name": "",
  "contractor_agency": ""
}

Rules:
- The project began in June 2026 and continues to the present. Reject any year before 2026. Never output 2020, 2021, 2022, 2023, 2024 or 2025.
- A short 3-digit year like "026" means 2026.
- Accept any clearly written date format (e.g. 4 September 2026, 04/09/2026, 2026-09-04).
- The form may write dates split by vertical bars, e.g. "31 | 08 | 026". Treat each "|" as a plain separator between day, month and year, never as a digit. A vertical stroke before a date is not the digit 1 - read "| 3/08/2026" as "03/08/2026", never "31/08/2026".
- For numeric dates with an ambiguous day/month order, use day-first order (DD/MM/YYYY) for this Indian form.
- Read every handwritten digit of the start and end date independently; never assume they are equal or consecutive.
- The survey runs at most 2-3 days, so start_date and end_date are always within 0-3 days of each other (they may be the same day). After reading both, check the gap: if it exceeds 3 days, one field is misread (usually a misread month or day digit on a blurry stroke) - re-read that field digit-by-digit before answering.
- If any date digit is ambiguous, lower its confidence below 0.85 so the record is flagged rather than guessed. Return null for a date that cannot be read confidently or is missing/blurry.
- If the Survey End Date is supplied to you as a known value (from the form's own text layer it cannot be misread), trust it exactly for end_date and read start_date so the pair is within 0-3 days of each other.
- For ae_ie_sc_name, piu_name and contractor_agency copy the printed value exactly as shown (keep the original spelling/case). Return null if a field is missing or unreadable.
- Do not guess, repair, or infer unclear values.
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
    contents.append("ENLARGED DATE HEADER:")
    contents.append(types.Part.from_bytes(data=header_bytes, mime_type="image/png"))
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

    # Blurry scans frequently misread a month or day digit. The survey window
    # (start/end at most MAX_START_END_GAP_DAYS apart) then catches the
    # impossible pair and a single focused re-read fixes it instead of storing
    # a wrong date.
    if not _dates_consistent(fields["start_date"], fields["end_date"]):
        fields = _repair_inconsistent_dates(
            fields, images, header_bytes, text_end_date, config
        )

    print(f"[GEMINI SURVEY FORM] parsed result: {fields}", flush=True)
    return fields


def _repair_inconsistent_dates(fields, images, header_bytes, text_end_date, config):
    """Attempt one focused re-read of a start/end pair >3 days apart.

    Such a gap cannot be a real NHAI field survey (they run 2-3 days max), so
    a blur misread a digit - most often the month. One extra Gemini call
    re-reads the handwritten dates with the window rule explicit. If the pair
    is still impossible afterwards, the lower-confidence field is dropped
    (ties keep the end date, which drives the defect-report delay and has the
    printed-text fallback) so a wrong value is never silently persisted.
    """
    start, end = fields["start_date"], fields["end_date"]
    print(
        f"[GEMINI SURVEY FORM] inconsistent date pair start={start} end={end} "
        f"gap > {MAX_START_END_GAP_DAYS}d - focused re-read",
        flush=True,
    )

    if text_end_date:
        anchor = (
            "The Survey End Date is already verified from the form's text "
            f"layer as {end}. Keep end_date = {end} and re-read ONLY the "
            f"handwritten Survey Start Date so the pair is within "
            f"{MAX_START_END_GAP_DAYS} days."
        )
    else:
        anchor = (
            "No date is externally verified. Re-read BOTH handwritten dates "
            "digit by digit from the enlarged header."
        )

    prompt = f"""
Return only JSON: {{"start_date": "", "start_confidence": 0.0, "end_date": "", "end_confidence": 0.0}}

The dates extracted in the previous pass were start={start} and end={end}.
On this NHAI road-survey form a survey lasts at most 2-3 days, so the Survey
Start Date and Survey End Date are never more than {MAX_START_END_GAP_DAYS} days
apart. One of the two readings is therefore wrong - typically a misread month
or day digit on a blurry pen stroke (e.g. "08" for "09").

{anchor}

Rules:
- Use DD/MM/YYYY for numeric dates. A short 3-digit year "026" means 2026. Reject any year before 2026.
- A vertical bar or stroke before a date is a separator, never the digit 1 ("| 3/09/2026" is "03/09/2026", not "31/09/2026").
- Read every handwritten digit independently. If a digit is still ambiguous, lower its confidence below 0.85.
- Confidence values must be between 0.0 and 1.0 and reflect visual certainty only.
"""

    retry_response = _safe_generate(
        [
            "FOCUSED DATE-PAIR RE-READ:",
            types.Part.from_bytes(data=header_bytes, mime_type="image/png"),
            types.Part.from_bytes(data=images[0], mime_type="image/png"),
            prompt,
        ],
        config,
    )

    retry_data = _parse_gemini_response(retry_response) or {}
    corrected = {
        "start_date": _valid_date(retry_data.get("start_date")),
        "start_confidence": _coerce_confidence(retry_data.get("start_confidence")),
        "end_date": _valid_date(retry_data.get("end_date")),
        "end_confidence": _coerce_confidence(retry_data.get("end_confidence")),
    }
    print(f"[GEMINI SURVEY FORM] re-read result: {corrected}", flush=True)

    if corrected["start_date"]:
        fields["start_date"] = corrected["start_date"]
        fields["start_confidence"] = corrected["start_confidence"]
    if corrected["end_date"] and not text_end_date:
        fields["end_date"] = corrected["end_date"]
        fields["end_confidence"] = corrected["end_confidence"]

    if _dates_consistent(fields["start_date"], fields["end_date"]):
        return fields

    # Still impossible after the re-read: never persist a pair that cannot be
    # true. Keep the more trustworthy field; ties keep the end date.
    print(
        "[GEMINI SURVEY FORM] date pair still inconsistent after re-read - "
        "dropping the lower-confidence field",
        flush=True,
    )
    if text_end_date:
        fields["start_date"] = None
        fields["start_confidence"] = None
    elif (fields["start_confidence"] or 0) > (fields["end_confidence"] or 0):
        fields["end_date"] = None
        fields["end_confidence"] = None
    else:
        fields["start_date"] = None
        fields["start_confidence"] = None
    return fields


def extract_survey_form_fields_from_drive(view_url):
    """Download a survey form from Drive and extract all fields in one call."""
    if view_url in _form_fields_cache:
        print("[GEMINI SURVEY FORM] using cached validated form fields", flush=True)
        return _form_fields_cache[view_url]

    with _EXTRACT_LOCK:
        print("[GEMINI SURVEY FORM] downloading survey form from Drive", flush=True)
        pdf_bytes = download_file_from_drive(view_url)
        print(f"[GEMINI SURVEY FORM] downloaded PDF bytes: {len(pdf_bytes)}", flush=True)

        fields = extract_survey_form_fields_from_pdf(pdf_bytes)
        _form_fields_cache[view_url] = fields
        return fields


def apply_survey_form_fields(survey, fields):
    """Assign extracted form fields onto a Survey object (does not commit).

    Only non-empty dates are written, so a field Gemini could not read stays
    None and is never overwritten by a stale value.
    """
    if fields.get("start_date"):
        survey.extracted_survey_start_date = date.fromisoformat(fields["start_date"])
        survey.survey_start_date_confidence = fields.get("start_confidence")
    if fields.get("end_date"):
        survey.extracted_survey_end_date = date.fromisoformat(fields["end_date"])
        survey.survey_end_date_confidence = fields.get("end_confidence")
    survey.extracted_ae_ie_sc_name = fields.get("ae_ie_sc_name")
    survey.extracted_piu_name = fields.get("piu_name")
    survey.extracted_contractor_agency = fields.get("contractor_agency")


_form_fields_cache = {}

