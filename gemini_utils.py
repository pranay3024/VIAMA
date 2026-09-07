import io
import json
import os
import re
from datetime import datetime
from typing import Optional

import fitz
import pikepdf
from google import genai
from google.genai import types
from pydantic import BaseModel

from google_drive import download_file_from_drive


# Validate API key
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not set in the environment.")

client = genai.Client(api_key=api_key)

# Updated default model to gemini-3.6-flash
MODEL_ID = (os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip()

_date_cache = {}


class SurveyDates(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def _valid_date(value):
    if not isinstance(value, str):
        return None

    value = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"[,]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    normalized = value.replace("/", "-").replace(".", "-")
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
            return datetime.strptime(normalized, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _get_first_page_image(pdf_bytes):
    """Extracts page 1 of the PDF without cropping and renders it as a PNG image."""
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
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        print(f"[GEMINI SURVEY DATES] Page rendering issue: {exc}", flush=True)
        return pdf_bytes


def extract_survey_dates_from_pdf(pdf_bytes):
    image_bytes = _get_first_page_image(pdf_bytes)

    prompt = """
Do not output any reasoning, chain of thought, explanations, or preamble. Output ONLY the extracted dates directly in JSON.

Extract the survey start date and survey end date from page 1 of this road survey form image.

Required JSON format:
{
  "start_date": "extracted start date",
  "end_date": "extracted end date"
}

Rules:
- Read the date beside the survey start/from label as start_date and the date beside the survey end/to label as end_date. Never swap them.
- Accept any clearly printed date format (e.g., 4 September 2026, 04/09/2026, 2026-09-04).
- For numeric dates with an ambiguous day/month order, use day-first order (DD/MM/YYYY) for this Indian survey form.
- Do not guess or repair unclear digits.
- Return null for missing, blurry, or uncertain dates.
"""

    # Note: gemini-3.6-flash enforces strict schema & JSON output via response_mime_type & response_schema.
    # Custom sampling settings (temperature, top_p, thinking_budget) are omitted to avoid parameter rejection.
    config = types.GenerateContentConfig(
        max_output_tokens=1024,
        response_mime_type="application/json",
        response_schema=SurveyDates,
    )

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            prompt,
        ],
        config=config,
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
        if not json_match:
            raise ValueError(f"Gemini returned invalid JSON: {text}")
        data = json.loads(json_match.group(0))

    normalized_data = {str(k).lower(): v for k, v in data.items()} if isinstance(data, dict) else {}

    dates = {
        "start_date": _valid_date(normalized_data.get("start_date")),
        "end_date": _valid_date(normalized_data.get("end_date")),
    }

    if not dates["start_date"] or not dates["end_date"]:
        raise ValueError(f"Gemini did not return two complete valid dates. Extracted: {dates}")

    print(f"[GEMINI SURVEY DATES] parsed result: {dates}", flush=True)
    return dates


def extract_survey_dates_from_drive(view_url):
    if view_url in _date_cache:
        print("[GEMINI SURVEY DATES] using cached validated dates", flush=True)
        return _date_cache[view_url]

    print("[GEMINI SURVEY DATES] downloading survey form from Drive", flush=True)
    pdf_bytes = download_file_from_drive(view_url)
    print(f"[GEMINI SURVEY DATES] downloaded PDF bytes: {len(pdf_bytes)}", flush=True)

    dates = extract_survey_dates_from_pdf(pdf_bytes)
    _date_cache[view_url] = dates
    return dates

