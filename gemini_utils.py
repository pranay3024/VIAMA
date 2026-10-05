import io
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
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

# The start and end dates on one form bracket a real field survey of a road
# stretch, which runs for days, not hours. sample.pdf reads 21/09/2026 ->
# 25/09/2026. The old 3-day ceiling was therefore not a heuristic that caught
# misreads - it rejected correct forms, and the repair path then deleted the
# start date even though both models read it identically at 0.95 confidence.
#
# Two separate questions are now kept apart:
#   * Ordering is definitional. An end date before the start date is
#     impossible, so it is a real error worth acting on.
#   * The size of the gap is only a soft signal. It buys one extra re-read when
#     the two dates are implausibly far apart; it never justifies discarding a
#     date that two independent passes read identically. Agreement between
#     passes is evidence, not a failure to be resolved by deletion.
MAX_SURVEY_SPAN_DAYS = 365

# Confidence at or above which a date the model actually read is kept even when
# it is the weaker half of a pair. Below this a reading is treated as a guess
# and may be dropped in favour of its partner.
DATE_CONFIDENCE_FLOOR = 0.5

# ---------------------------------------------------------------------------
# Where the dates are on the page
# ---------------------------------------------------------------------------

#: Printed row labels that mark where the handwritten dates live, grouped into
#: tiers of decreasing specificity.  A tier is only used if every tier above it
#: matched nothing, so "End Date" wins over a bare "Date" and we never mix the
#: two.
#:
#: Full phrases only, deliberately.  ``search_for("To")`` matches "Total",
#: "Introduction" and every other word containing the letters, which would anchor
#: the crop to an arbitrary part of the page - worse than the fixed bands this
#: replaces.  Multi-word labels are also far more likely to be a real form field
#: than a two-letter fragment.
DATE_LABEL_ANCHOR_TIERS = (
    ("Survey End Date", "Survey Start Date", "Survey From", "Survey To"),
    ("End Date", "Start Date", "Date of Survey"),
    ("Date",),
)

#: Padding around a label rect, in multiples of that label's own width/height.
#: Generous horizontally because a fill-in-the-box date is written well to the
#: right of its label, and vertically because the writing sits on or just above
#: the label's baseline and its height varies.
#:
#: These were 1.5 / 3.0 / 9.0. The vertical 3.0x is what turned a 21pt label into
#: a 150pt band, so each of the three overlapping "Survey End Date" hits produced
#: its own near-identical full-height crop - three tiles billed for one row of
#: handwriting. Handwriting overshoots its label by roughly half a label height,
#: which 1.5x covers with room to spare.
DATE_LABEL_PAD_X = 0.6
DATE_LABEL_PAD_Y = 1.5
DATE_LABEL_PAD_X_TRAILING = 6.0

#: Upper bound on label-anchored crops per page *after* overlapping bands have
#: been merged.  Each one is another 768px tile bill, and a form has at most a
#: couple of date rows; two is generous once the start and end rows have been
#: unioned into the single band they usually share.
MAX_DATE_LABEL_CLIPS = 2

#: Zoom for a label-anchored crop.  Higher than the full-page view because the
#: region is much smaller, so the tile count stays about the same while the
#: handwriting is rendered larger.
DATE_LABEL_ZOOM = 4

#: Zoom for the whole-page floor view.  3x on a letter page is ~3x4 tiles,
#: which is what the two overlapping 55%/45% bands used to cost between them.
#: It is the lowest zoom at which small handwriting on a scan stays readable,
#: so it is the floor, not a tunable.
DATE_PAGE_ZOOM = 3

#: Zoom for the locate-then-zoom second pass.  Only ever used on a small region
#: the model has already pointed at, so the tile cost is bounded.  This is the
#: zoom at which a handwritten 9 stops looking like a 3.
DATE_FIELD_ZOOM = 6

#: Slack on the future edge of the plausibility window.  A form can legitimately
#: be dated a day or two ahead - pre-filled, or a scanner/Gemini clock a little
#: out - and rejecting it outright loses a good reading over a technicality.
DATE_FUTURE_GRACE_DAYS = 3


def _dates_consistent(start_iso, end_iso):
    """True unless the pair is impossible: an end date before the start date.

    The gap between the two dates is deliberately not checked here. A survey
    brackets a whole stretch of road and can span several days - see
    ``MAX_SURVEY_SPAN_DAYS``. The old version enforced a 3-day ceiling here,
    which flagged correct readings for repair, and the repair path then
    deleted whichever date it decided was weakest.
    """
    if not start_iso or not end_iso:
        return True
    return date.fromisoformat(start_iso) <= date.fromisoformat(end_iso)


def _span_is_plausible(start_iso, end_iso):
    """True unless the two dates are too far apart to belong to one survey.

    Used only to decide whether an extra re-read is worth paying for. Being
    False never discards a date on its own.
    """
    if not start_iso or not end_iso:
        return True
    gap = (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days
    return 0 <= gap <= MAX_SURVEY_SPAN_DAYS


def _enhance_header(png_bytes):
    """Contrast-boost and sharpen an enlarged date-header crop.

    Zooming a blurry scan further cannot invent detail the scan never had;
    autocontrast plus an unsharp mask recovers the soft edges of a blurred
    pen stroke, which is what the vision model keys digit boundaries on.

    Works in grayscale.  Survey forms are filled in with blue and black ballpoint
    and scanned, so a colour crop carries a chroma channel that is pure scanner
    noise; dropping it means the unsharp mask acts on luminance alone, which is
    the channel the digit edges actually live in.

    ``autocontrast(cutoff=1)`` rather than the default 0.  With a cutoff of 0 the
    single darkest speck and single lightest pixel get stretched to the extremes,
    which amplifies JPEG blocking and paper speckle along with the strokes.  A
    1% clip keeps the histogram honest.

    Best-effort: any failure returns the original bytes untouched.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps

        image = Image.open(io.BytesIO(png_bytes)).convert("L")
        image = ImageOps.autocontrast(image, cutoff=1)
        image = image.filter(
            ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] header enhancement skipped: {exc}", flush=True)
        return png_bytes


def _merge_overlapping_rects(rects):
    """Union bands that overlap, so one region of a page is cropped only once.

    ``search_for`` returns one rect per hit, and a label like "Survey End Date"
    that is drawn with a colon or split across text runs comes back as two or
    three slightly offset rects. Padded, they overlap almost completely, so
    without this the caller pays for three crops of one row of handwriting -
    which is what happened on sample.pdf.

    Merging is transitive, so a chain of partially overlapping bands collapses
    into a single covering rect. Order is preserved top-to-bottom.
    """
    if not rects:
        return []

    ordered = sorted(rects, key=lambda r: r.y0)
    merged = [fitz.Rect(ordered[0])]
    for rect in ordered[1:]:
        current = merged[-1]
        if rect.y0 <= current.y1 and rect.x0 <= current.x1:
            merged[-1] = current | rect
        else:
            merged.append(fitz.Rect(rect))
    return merged


def _label_anchored_date_clips(page, max_clips=MAX_DATE_LABEL_CLIPS):
    """
    Rects tightly around the form's printed date labels, or ``[]`` if it has none.

    This is what makes the crop reliable.  A fixed band (``height * 0.30`` and
    friends) assumes the dates sit in the same slice of every form, and a survey
    form that moves its date row even slightly - or writes the date in the
    bottom sign-off block, which the prompts already acknowledge happens - falls
    outside it and Gemini is never shown the digits.

    Anchoring on the printed label instead means the crop follows the form, so
    the same code works whether the dates are at the top, the middle or the
    bottom.  Returns ``[]`` rather than guessing when no label is found, which
    lets the caller fall back to the fixed bands.

    Only page 1 is searched: the form fields all live there and every other page
    is already sent whole.
    """
    page_rect = page.rect
    height = max(page_rect.height, 1.0)

    for tier in DATE_LABEL_ANCHOR_TIERS:
        bands = []

        for needle in tier:
            try:
                hits = page.search_for(needle) or []
            except Exception as exc:
                print(
                    f"[GEMINI SURVEY FORM] label search for {needle!r} failed: {exc}",
                    flush=True,
                )
                continue

            for rect in hits:
                # Handwriting is written on the label's line, so pad vertically by
                # the label's own height and horizontally far more to the right,
                # where a fill-in-the-box date sits.
                band = fitz.Rect(
                    max(page_rect.x0, rect.x0 - rect.width * DATE_LABEL_PAD_X),
                    max(page_rect.y0, rect.y0 - rect.height * DATE_LABEL_PAD_Y),
                    min(
                        page_rect.x1,
                        rect.x1 + rect.width * DATE_LABEL_PAD_X_TRAILING,
                    ),
                    min(page_rect.y1, rect.y1 + rect.height * DATE_LABEL_PAD_Y),
                )

                # A label that is really the whole page would produce a useless
                # full-page "zoom"; the bands are for rows, not documents.
                if band.height > height * 0.6 or band.width < 1 or band.height < 1:
                    continue

                bands.append(band)

        if bands:
            # The start and end date rows are normally adjacent, so after
            # merging the tier usually collapses to a single band covering both.
            clips = _merge_overlapping_rects(bands)
            clips.sort(key=lambda r: r.y0)
            return clips[:max_clips]

    return []


def _render_date_crops(page, clips, zoom):
    """Render ``clips`` to enhanced PNG bytes, skipping any that come out empty."""
    crops = []
    for index, clip in enumerate(clips, start=1):
        try:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(zoom, zoom),
                clip=clip,
                alpha=False,
            )
        except Exception as exc:
            print(
                f"[GEMINI SURVEY FORM] date crop {index} render failed: {exc}",
                flush=True,
            )
            continue
        if not pixmap.width or not pixmap.height:
            continue
        crops.append(_enhance_header(pixmap.tobytes("png")))
    return crops


# ---------------------------------------------------------------------------
# Locate-then-zoom
# ---------------------------------------------------------------------------
#
# Why this exists. Survey forms come in more than one shape, and on a form with
# no text layer the printed labels cannot be searched for, so the only thing
# sent is the whole page. At whole-page zoom a handwritten 9 and a handwritten
# 3 are the same grey smudge: on sample2.pdf the start date reads as
# 25/03/2026 across the full page and as 25/09/2026 - correctly - in every
# close-up. Six independent close-up reads agreed on 09.
#
# So when a date field comes back unusable, do not just ask the same coarse
# question again. Ask the model where the date fields are, crop exactly there,
# and read the digits at a zoom where they are actually distinguishable.

#: Fields we can go looking for.
LOCATE_TARGET_FIELDS = ("start_date", "end_date")

#: Cap on located regions rendered per pass. Each is a 6x crop, so this bounds
#: the extra cost of a second pass.
MAX_LOCATED_CROPS = 4

#: Fraction of the page height a located box is expanded by, so a tight box
#: still contains the whole label, the value and any stroke that overshoots.
LOCATE_BOX_PAD = 0.02


class DateFieldBox(BaseModel):
    """Where one date value sits, as a fraction of the page.

    The two components are named rather than a bare ``[y, x]`` pair on purpose.
    A positional pair forces the caller to guess which component is which, and
    any such guess is a coin flip that silently transposes a genuinely correct
    answer whenever the field happens to sit in the top-left of the page.
    """

    y_frac: float
    x_frac: float


class DateFieldRegions(BaseModel):
    """Where the date fields are on page 1."""

    start_date_box: Optional[DateFieldBox] = None
    end_date_box: Optional[DateFieldBox] = None


#: Largest value accepted from a locate response. Gemini reports grounding
#: coordinates on a 0-1000 scale, so a response in per-mille is expected and
#: normalised rather than discarded.
LOCATE_COORD_MAX = 1000.0


def _normalize_locate_coordinate(value):
    """Map a reported coordinate onto 0.0-1.0, or None if it is not one.

    Models report locate coordinates in either of two scales: as page
    fractions (0.0-1.0), or on Gemini's native 0-1000 grounding scale. Both
    occur in practice - on sample2.pdf the model answered
    ``start_date_box: [177, 880]``, which is per-mille, and that is a 0.177 /
    0.880 hit against a date measured at 0.174 / 0.875. Rejecting it as "out of
    range" would throw away an excellent localisation, so normalise instead.

    The two scales are not ambiguous: any component above 1.0 cannot be a
    fraction, so it is per-mille. Normalising per component also lets a
    response mix scales, which happens when one axis is reported in per-mille
    and the other as a fraction.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0.0:
        return None
    if number <= 1.0:
        return number
    if number <= LOCATE_COORD_MAX:
        return number / LOCATE_COORD_MAX
    return None


def _fractions_to_rect(page, box):
    """Turn a reported box into a clipped rect on ``page``, or None.

    Returns None for anything that is not a usable box, so a hallucinated or
    malformed response cannot produce a nonsense crop.
    """
    y_frac = x_frac = None
    if isinstance(box, dict):
        y_frac = _normalize_locate_coordinate(box.get("y_frac"))
        x_frac = _normalize_locate_coordinate(box.get("x_frac"))
    elif isinstance(box, (list, tuple)) and len(box) == 2:
        # Positional fallback for a model that ignores the schema. The documented
        # order is [y, x] and it is honoured verbatim: no "which one is taller"
        # heuristic, because that heuristic corrupts a correct answer whenever
        # the field genuinely sits in the top-left of the page, and a misplaced
        # crop is no better than no crop.
        y_frac = _normalize_locate_coordinate(box[0])
        x_frac = _normalize_locate_coordinate(box[1])
    if y_frac is None or x_frac is None:
        return None

    rect = page.rect
    width = rect.width
    height = rect.height
    pad_y = height * LOCATE_BOX_PAD
    pad_x = width * LOCATE_BOX_PAD
    crop = fitz.Rect(
        max(rect.x0, x_frac * width - pad_x),
        max(rect.y0, y_frac * height - pad_y),
        min(rect.x1, x_frac * width + pad_x),
        min(rect.y1, y_frac * height + pad_y),
    )
    # Padding is what makes a tight box usable, so a box the padding cannot
    # open up is a coordinate the model got wrong rather than a tight field.
    if crop.is_empty or crop.get_area() < 1.0:
        return None
    return crop


def _locate_date_field_rects(page):
    """Ask the model where the date fields are on ``page``; return rects.

    Returns ``[]`` when the model cannot point at anything, in which case the
    caller keeps whatever it already has. Never raises: this is an optimisation
    on top of a path that already works, so failing to locate must not fail the
    extraction.
    """
    try:
        overview = page.get_pixmap(
            matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, alpha=False
        ).tobytes("png")
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] locate overview render failed: {exc}", flush=True)
        return []

    prompt = """
Locate the survey date fields on this page. Do NOT read or transcribe the dates - only report where they are.

For each date field, report the centre of the value written in that field as a fraction of the page, where 0.0 is the top/left edge and 1.0 is the bottom/right edge:
- "start_date_box": {"y_frac": <y>, "x_frac": <x>} for the Survey Start Date / From value
- "end_date_box": {"y_frac": <y>, "x_frac": <x>} for the Survey End Date / To value

Report a field as null if that field is not on the page, or is on the page but left empty.
Do not point at a printed reference or circular number - only the handwritten survey start and end dates.
Return JSON only.
"""

    # This must build its own config. Reusing the caller's config would apply
    # the SurveyFormFields response schema, which pins the reply to the field
    # set and leaves no room for the boxes at all.
    config = types.GenerateContentConfig(
        max_output_tokens=512,
        response_mime_type="application/json",
        response_schema=DateFieldRegions,
    )

    try:
        response = _safe_generate(
            [
                "PAGE OVERVIEW FOR LOCATING THE DATE FIELDS:",
                types.Part.from_bytes(data=overview, mime_type="image/png"),
                prompt,
            ],
            config,
        )
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] date-field locate failed: {exc}", flush=True)
        return []

    data = _parse_gemini_response(response) or {}
    rects = []
    for field in LOCATE_TARGET_FIELDS:
        box = data.get(f"{field}_box")
        rect = _fractions_to_rect(page, box)
        if rect is None:
            print(
                f"[GEMINI SURVEY FORM] locate returned no usable box for {field}",
                flush=True,
            )
            continue
        print(
            f"[GEMINI SURVEY FORM] located {field} at "
            f"y {rect.y0 / page.rect.height:.3f}-{rect.y1 / page.rect.height:.3f} "
            f"x {rect.x0 / page.rect.width:.3f}-{rect.x1 / page.rect.width:.3f}",
            flush=True,
        )
        rects.append(rect)

    return _merge_overlapping_rects(rects)[:MAX_LOCATED_CROPS]


def _located_date_zoom_views(pdf_bytes):
    """Enlarged close-ups of the date cells, located by the model.

    Opens the PDF, asks where the date fields are, and renders just those
    regions at ``DATE_FIELD_ZOOM``. Returns ``[]`` on any failure - this only
    sharpens a re-read that already has the whole page to work from, so it must
    never be the thing that fails.
    """
    if not pdf_bytes:
        return []
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = document[0]
        rects = _locate_date_field_rects(page)
        if not rects:
            print(
                "[GEMINI SURVEY FORM] no date field located; re-reading from "
                "the whole-page views only",
                flush=True,
            )
            return []
        return _render_date_crops(page, rects, DATE_FIELD_ZOOM)
    except Exception as exc:
        print(f"[GEMINI SURVEY FORM] located date zoom failed: {exc}", flush=True)
        return []


def _page_date_views(page):
    """High-resolution views of page 1, aimed at the date rows.

    The date section must never be cropped out. Survey forms come in several
    layouts - a text-backed form with printed "Survey Start Date:" labels, a
    pure scan with no text layer at all, one that signs off with a date in the
    bottom block - so no fixed percentage band can be relied on to contain the
    digits. Anything band-based is a bet that the dates happen to sit in that
    slice of *this* form.

    So the floor is a single continuous view of the whole page at high zoom.
    It cannot omit a region by construction, which is the guarantee we need,
    and it costs exactly what the two overlapping bands it replaces cost: a
    letter page at 3x is ~3x4 tiles, and so were the 55% and 45% bands.

    On top of that floor sit label-anchored crops when the form has a text
    layer. They are tighter and cheaper than the full page, so they go first
    and the model sees the date rows magnified before it sees everything else.
    """
    views = []

    anchored = _render_date_crops(
        page, _label_anchored_date_clips(page), DATE_LABEL_ZOOM
    )
    if anchored:
        print(
            f"[GEMINI SURVEY FORM] {len(anchored)} label-anchored date crop(s)",
            flush=True,
        )
        views.extend(anchored)

    # Unconditional full-page floor. One continuous view, not bands: a band can
    # miss the date rows, and two bands leave a seam where a row can be split
    # across both and read as two half-dates.
    full = _render_date_crops(page, (page.rect,), DATE_PAGE_ZOOM)
    if full:
        views.extend(full)
        if not anchored:
            print(
                "[GEMINI SURVEY FORM] no text layer to anchor on; sending the "
                "whole page at high zoom so the date section cannot be missed",
                flush=True,
            )

    return views


def _parse_date(value):
    """Return any calendar-valid date in ``value`` as an ISO string, else None.

    Pure parsing: no opinion about whether the date is plausible for this
    project. Kept separate from :func:`_valid_date` because collapsing "I could
    not read this" and "I read it but it is out of range" into the same None
    destroys the information the re-read needs - the caller cannot tell the
    model that its reading was rejected and why.
    """
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
            return datetime.strptime(normalized, date_format).date().strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _valid_date(value):
    """A date the model read, if it is also plausible for this project.

    This is what gets stored, so the window is enforced here and not merely
    logged. It is enforced rather than warned about because
    ``defect_report_delay_days`` treats the end date as the start of the delay
    window: storing a hallucinated 2019 date would produce a nonsense delay
    rather than a missing one. The rejected value is not thrown away, though -
    :func:`_extract_survey_form_fields_from_pdf` keeps it as a hint for the
    re-read, which is what lets a misread month get corrected.
    """
    parsed = _parse_date(value)
    if parsed is None:
        return None
    as_date = date.fromisoformat(parsed)
    if as_date < MIN_SURVEY_DATE:
        return None
    if as_date > MAX_SURVEY_DATE + timedelta(days=DATE_FUTURE_GRACE_DAYS):
        return None
    return parsed


def _rejected_reading(value):
    """A parsable date that failed the project window, as ``(raw, iso)`` or None.

    Lets the caller tell "the model read 25/03/2026 and that is outside the
    project" apart from "the model returned nothing". The first is worth a
    second, informed look; the second is not.
    """
    parsed = _parse_date(value)
    if parsed is None or _valid_date(value) is not None:
        return None
    return (value.strip(), parsed)


def _get_first_page_images(pdf_bytes):
    """Render page 1 whole, plus the high-resolution date views of that page.

    Returns ``(full_page_png, [date_view_png, ...])``. The date views come from
    the same label-anchored helper the full form-field path uses, so the two
    entry points can no longer disagree about where the dates are. This used to
    return a single hard-coded top-30% crop.
    """
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
        # and spend the remaining tiles on the date rows.
        full_page = page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            alpha=False,
        )
        date_views = _page_date_views(page)
        return full_page.tobytes("png"), (date_views or [full_page.tobytes("png")])
    except Exception as exc:
        print(f"[GEMINI SURVEY DATES] Page rendering issue: {exc}", flush=True)
        return pdf_bytes, [pdf_bytes]


#: Labels whose following text holds the corresponding date, keyed by the
#: field they fill. Checked in order; the first label that yields a parsable
#: date wins, so the specific "Survey End Date" beats a bare "End Date".
TEXT_LAYER_DATE_LABELS = (
    ("end_date", ("survey\\s*end\\s*date", "end\\s*date", "survey\\s*to")),
    ("start_date", ("survey\\s*start\\s*date", "start\\s*date", "survey\\s*from")),
)

#: How far past a label to look for its value. Wide enough for a form that puts
#: the date on the next text line, narrow enough that the date belongs to this
#: field rather than to the one below it.
TEXT_LAYER_LOOKAHEAD = 60

_DATE_CANDIDATE_RE = (
    r"\b\d{1,2}\s*[/|.-]\s*\d{1,2}\s*[/|.-]\s*\d{2,4}\b"
    r"|\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{4}\b"
)


def _extract_dates_from_pdf_text(pdf_bytes):
    """Read machine-printed date strings out of page 1's text layer.

    Returns ``{}`` or a dict with any of ``start_date`` / ``end_date``.

    Deliberately a *corroboration and fallback* source, never an authority. The
    old version stamped whatever it found with ``confidence = 1.0`` and let it
    override the vision reading unconditionally, on the theory that "the text
    layer cannot be misread the way handwriting can". That is backwards for a
    scanned form: the text layer of a scan is an OCR transcript, and OCR of
    handwriting is a strictly lossier view of the glyphs than the image is.
    sample.pdf carries the text ``25|01 202`` immediately after
    "Survey End Date:" - garbage that the old code was one regex tweak away
    from promoting over a correct visual read at full confidence.

    It also only ever looked for an *end* date, so a form with a machine-printed
    start date got nothing from this path at all.
    """
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = document[0].get_text("text")
    except Exception:
        return {}

    found = {}
    for field, label_patterns in TEXT_LAYER_DATE_LABELS:
        for label_pattern in label_patterns:
            label_match = re.search(label_pattern, text, flags=re.IGNORECASE)
            if not label_match:
                continue

            nearby_text = text[label_match.end():label_match.end() + TEXT_LAYER_LOOKAHEAD]
            # Prefer the label's own line - that is where a filled-in date
            # sits - but fall back to the whole window for forms that lay the
            # value out on the following line.
            windows = [nearby_text.split("\n", 1)[0] or nearby_text, nearby_text]
            for window in windows:
                for candidate in re.findall(
                    _DATE_CANDIDATE_RE, window, flags=re.IGNORECASE
                ):
                    normalized = _valid_date(candidate)
                    if normalized:
                        found[field] = normalized
                        break
                if field in found:
                    break
            if field in found:
                break

    if found:
        print(
            f"[GEMINI SURVEY FORM] text-layer dates (corroboration only): {found}",
            flush=True,
        )
    return found


def extract_survey_dates_from_pdf(pdf_bytes):
    text_dates = _extract_dates_from_pdf_text(pdf_bytes)
    if text_dates.get("end_date"):
        dates = {"end_date": text_dates["end_date"], "end_confidence": 1.0}
        print(
            f"[GEMINI SURVEY DATES] parsed text-layer result: {dates}",
            flush=True,
        )
        return dates

    full_page_bytes, date_views = _get_first_page_images(pdf_bytes)

    prompt = """
Do not output any reasoning, chain of thought, explanations, or preamble. Output ONLY the extracted dates directly in JSON.

Extract only the survey end date from page 1 of this road survey form.
Several views are supplied: the complete page and enlarged crops of the date
rows, which are anchored on the form's own printed date labels.

Required JSON format:
{
    "end_date": "extracted end date",
    "end_confidence": 0.0
}

Rules:
- Read only the date beside the Survey End Date/To label. Never copy the Survey Start Date as the end date.
- Read every handwritten digit in the Survey End Date row independently; never copy the Survey Start Date.
- The end date may be different from the start date. Do not assume they are equal or consecutive.
- The survey brackets a real stretch of road, so the end date is normally several days after the start date shown next to it. Do not treat a multi-day gap as evidence that you have misread a digit, and do not change a clearly written digit to shrink the gap.
- If any end-date digit is ambiguous, return the end date with confidence below 0.85 so the record is flagged for review rather than silently guessed.
- Inspect the enlarged date crops carefully, including handwritten digits.
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

    contents = ["COMPLETE FIRST PAGE:",
                types.Part.from_bytes(data=full_page_bytes, mime_type="image/png")]
    for index, date_view in enumerate(date_views, start=1):
        contents.append(f"HIGH-RESOLUTION DATE VIEW {index}:")
        contents.append(types.Part.from_bytes(data=date_view, mime_type="image/png"))
    contents.append(prompt)

    response = _safe_generate(contents, config)

    data = _parse_gemini_response(response)
    if data is None:
        raise ValueError("Gemini returned unparseable JSON for the survey end date")

    normalized_data = {str(k).lower(): v for k, v in data.items()}

    dates = {
        "end_date": _valid_date(normalized_data.get("end_date")),
        "end_confidence": float(normalized_data.get("end_confidence") or 0),
    }

    if not dates["end_date"]:
        retry_prompt = """
Return only JSON for the survey end date visible in the supplied images.
Look specifically at the row labelled Survey End Date or To. Do not use the
Survey Start Date. Read the date exactly as written, including day, month and
year. The survey spans several days, so the end date is normally later than the
start date; do not alter a visible digit to bring them closer together. Use
DD/MM/YYYY for numeric dates. Return null only if the end-date row is not
readable.
"""
        retry_contents = ["FOCUSED END-DATE RETRY:"]
        for date_view in date_views:
            retry_contents.append(
                types.Part.from_bytes(data=date_view, mime_type="image/png")
            )
        retry_contents.append(
            types.Part.from_bytes(data=full_page_bytes, mime_type="image/png")
        )
        retry_contents.append(retry_prompt)
        retry_response = _safe_generate(retry_contents, config)
        retry_data = _parse_gemini_response(retry_response) or {}
        dates = {
            "end_date": _valid_date(retry_data.get("end_date")),
            "end_confidence": float(retry_data.get("end_confidence") or 0),
        }

    if not dates["end_date"]:
        raise ValueError(f"Gemini did not return a valid end date. Extracted: {dates}")

    if dates["end_confidence"] < DATE_CONFIDENCE_FLOOR:
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
        date_views = _page_date_views(document[0]) if pages else []
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

    text_dates = _extract_dates_from_pdf_text(pdf_bytes)

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
- The whole of page 1 is supplied at high zoom, so every date field is visible whatever layout this form uses. The handwritten dates may be in the top header, the middle of the form, or the bottom sign-off area.
- Survey Start Date / From means start_date. Survey End Date / To means end_date. Never copy one row's date into the other row and never use a date from another page.
- Read every handwritten digit independently. Faint, small, or imperfect handwriting is still a date to transcribe; do not return null merely because the writing is handwritten or low contrast. If a digit is uncertain, return the best visual reading and set that date's confidence below 0.85.
- Use null for a date only when its labelled row has no readable date at all. When a date is null, set its confidence to 0.0.
- Transcribe exactly what is written. Do not adjust a digit to make a date fall inside a plausible range; an implausible reading is more useful to us than a plausible invention.
- The project began in June 2026. Reject a year before 2026. A short year "026" means 2026.
- Accept formats such as 4 September 2026, 04/09/2026, 2026-09-04, 04-09-026, and dates separated by vertical bars. Treat a bar or stroke as a separator, not as the digit 1.
- For numeric dates with an ambiguous day/month order, use day-first DD/MM/YYYY for this Indian form.
- The survey brackets a real stretch of road, so the end date is normally several days after the start date. A multi-day gap is expected and is NOT a reason to doubt a digit. The only ordering that is impossible is an end date earlier than the start date.

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

    if text_dates:
        prompt = prompt + (
            "\nThe form's machine-printed text layer contains the string(s) "
            f"{text_dates}. Treat these as a weak hint only: the text layer of a "
            "scan is an OCR transcript and can be wrong, so verify them against "
            "the images and prefer what you can actually see.\n"
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

    # A reading the model made but that fell outside the project window is a
    # different thing from no reading at all, and the re-read has to be able to
    # tell them apart. On sample2.pdf the model read the start date confidently
    # as 25/03/2026 - a misread 9 as a 3 - and the window check turned that into
    # a bare null, so the retry was told nothing and reproduced the same
    # misread. Keeping the rejected string turns that blind retry into an
    # informed one.
    rejected = {
        field: _rejected_reading(normalized.get(field))
        for field in ("start_date", "end_date")
    }
    rejected = {k: v for k, v in rejected.items() if v}
    for field, (raw, iso) in rejected.items():
        print(
            f"[GEMINI SURVEY FORM] {field} read as {raw!r} ({iso}) but that is "
            f"outside the project window {MIN_SURVEY_DATE}..{MAX_SURVEY_DATE}; "
            "keeping it as a hint for the re-read",
            flush=True,
        )

    fields = {
        "start_date": _valid_date(normalized.get("start_date")),
        "start_confidence": _coerce_confidence(normalized.get("start_confidence")),
        "end_date": _valid_date(normalized.get("end_date")),
        "end_confidence": _coerce_confidence(normalized.get("end_confidence")),
        "ae_ie_sc_name": _clean_text(normalized.get("ae_ie_sc_name")),
        "piu_name": _clean_text(normalized.get("piu_name")),
        "contractor_agency": _clean_text(normalized.get("contractor_agency")),
    }

    # The text layer is a fallback for a date the vision pass could not read at
    # all, not a veto over what it did read. Overriding here - and stamping the
    # result confidence 1.0 - let a single bad OCR character beat a correct
    # visual reading of clearly legible handwriting.
    for field in ("start_date", "end_date"):
        if not fields[field] and text_dates.get(field):
            fields[field] = text_dates[field]
            fields[f"{field.removesuffix('_date')}_confidence"] = 0.8
            print(
                f"[GEMINI SURVEY FORM] {field} unread by vision; using text "
                f"layer {fields[field]} at reduced confidence",
                flush=True,
            )

    needs_date_repair = (
        not fields["start_date"]
        or not fields["end_date"]
        or not _dates_consistent(fields["start_date"], fields["end_date"])
        or not _span_is_plausible(fields["start_date"], fields["end_date"])
    )
    if needs_date_repair:
        fields = _repair_inconsistent_dates(
            fields, images, date_views, text_dates, rejected, config, pdf_bytes
        )

    print(f"[GEMINI SURVEY FORM] parsed result: {fields}", flush=True)
    return fields


def _repair_inconsistent_dates(
    fields, images, date_views, text_dates, rejected, config, pdf_bytes=None
):
    start = fields.get("start_date")
    end = fields.get("end_date")
    missing_start = not start
    missing_end = not end
    inverted = bool(start and end and not _dates_consistent(start, end))
    implausible_span = bool(
        start and end and _dates_consistent(start, end)
        and not _span_is_plausible(start, end)
    )
    print(
        f"[GEMINI SURVEY FORM] date re-read start={start} end={end} "
        f"missing_start={missing_start} missing_end={missing_end} "
        f"inverted={inverted} implausible_span={implausible_span}",
        flush=True,
    )

    if start and missing_end:
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

    # Tell the model what it said last time when that reading was rejected for
    # falling outside the project window. Without this the re-read has no idea
    # its previous answer was discarded, and just produces it again.
    hint = ""
    if rejected:
        lines = "\n".join(
            f"  - {field}: you read it as {raw!r} ({iso}), which falls outside "
            f"the project window {MIN_SURVEY_DATE} to {MAX_SURVEY_DATE}, so we "
            "discarded it. That is usually one misread digit rather than a "
            "genuinely impossible date - look again at the enlarged crops."
            for field, (raw, iso) in sorted(rejected.items())
        )
        hint = f"""
Your previous reading was rejected. Specifically:
{lines}

The single most common cause is a month digit read as 3 instead of 9 (or the
reverse): a 9 has a closed loop at the top, a 3 is open on the upper left.
Check every month digit against that distinction in the enlarged crops.
"""

    prompt = f"""
Return only JSON with these keys: start_date, start_confidence, end_date, end_confidence.

This is a focused re-read of an NHAI road-survey form. The whole of page 1 is
supplied, plus enlarged close-ups of the date fields. Survey Start Date / From is
start_date and Survey End Date / To is end_date. The previous pass returned
start={start} and end={end}.

{anchor}
{hint}
Read the actual handwriting in the enlarged views. Faint or imperfect
handwriting is still a date; return the best visual transcription and a
confidence below 0.85 if a digit is uncertain. Do not return null merely
because the writing is handwritten, small, or low contrast. Use null only when
the labelled row has no readable date. Read the day, month, and year digits
independently. Use DD/MM/YYYY for numeric dates, treat 026 as 2026, and treat
vertical bars or strokes as separators rather than digits. The survey spans
several days, so the end date being days after the start date is normal and is
not a reason to alter a visible digit. The only impossible ordering is an end
date earlier than the start date. Transcribe what is written; do not adjust a
digit to move a date into a plausible range.
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

    # Locate-then-zoom. Close-ups of the actual date cells, at a zoom where the
    # digit shapes are separable, are what resolve a month misread at whole-page
    # zoom. Best-effort: if locating fails we still have the page views above.
    for index, zoom_view in enumerate(
        _located_date_zoom_views(pdf_bytes), start=1
    ):
        contents.append(f"ENLARGED DATE FIELD {index} (read the digits here):")
        contents.append(
            types.Part.from_bytes(data=zoom_view, mime_type="image/png")
        )

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

    # The re-read can be rejected by the same window check that rejected the
    # first pass. Surface it rather than reporting the field as blank, so the
    # logs distinguish "nothing there" from "read but not storable".
    retry_rejected = {
        field: _rejected_reading(retry_data.get(field))
        for field in ("start_date", "end_date")
    }
    for field, finding in retry_rejected.items():
        if finding:
            raw, iso = finding
            print(
                f"[GEMINI SURVEY FORM] re-read {field}={raw!r} ({iso}) is still "
                "outside the project window; leaving the field blank for manual "
                "entry",
                flush=True,
            )

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

    # A missing date is always worth taking from the re-read. An existing one is
    # replaced only when the re-read produced a different value, which is the
    # signal that the first pass misread something.
    for key in ("start_date", "end_date"):
        confidence_key = f"{key.removesuffix('_date')}_confidence"
        if not corrected[key]:
            continue
        if not fields[key] or corrected[key] != fields[key]:
            print(
                f"[GEMINI SURVEY FORM] re-read {key}: "
                f"{fields[key]!r} -> {corrected[key]!r}",
                flush=True,
            )
            fields[key] = corrected[key]
            fields[confidence_key] = corrected[confidence_key]

    if not fields["start_date"]:
        fields["start_confidence"] = None
    if not fields["end_date"]:
        fields["end_confidence"] = None

    if _dates_consistent(fields["start_date"], fields["end_date"]):
        return fields

    # The two dates are in an impossible order: an end date before the start
    # date. Both passes agree on the values, so at least one row was mislabelled
    # by the reader. Only discard a date that the model itself is unsure of -
    # never one it read confidently, and never on a tie, which the old
    # comparison collapsed into "always drop the start date".
    print(
        "[GEMINI SURVEY FORM] date pair is in an impossible order after re-read",
        flush=True,
    )
    start_confidence = fields["start_confidence"] or 0.0
    end_confidence = fields["end_confidence"] or 0.0

    if start_confidence < end_confidence:
        weakest, weakest_confidence = "start_date", start_confidence
    elif end_confidence < start_confidence:
        weakest, weakest_confidence = "end_date", end_confidence
    else:
        # Equal confidence: the readings are equally trustworthy, so dropping
        # either one is arbitrary. Keep both and let the admin review flag it,
        # which is what the confidence column is for.
        print(
            "[GEMINI SURVEY FORM] both dates are equally confident and out of "
            "order; keeping both for review rather than discarding one",
            flush=True,
        )
        return fields

    if weakest_confidence >= DATE_CONFIDENCE_FLOOR:
        print(
            f"[GEMINI SURVEY FORM] out-of-order {weakest} was read confidently; "
            "keeping it for review rather than discarding it",
            flush=True,
        )
        return fields

    fields[weakest] = None
    fields[f"{weakest.removesuffix('_date')}_confidence"] = None
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
    if fields.get("start_date"):
        survey.extracted_survey_start_date = date.fromisoformat(
            fields["start_date"]
        )
        survey.survey_start_date_confidence = fields.get("start_confidence")
    if fields.get("end_date"):
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

