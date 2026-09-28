"""
Offline regression tests for survey-form date extraction.

Run with:  python test_date_extraction.py

No network calls: every Gemini interaction is stubbed. Two real forms drive
the layout assertions:

- sample2.pdf is a four page pure scan with no text layer at all. It reads
  Survey Start Date 25/09/2026 and Survey End Date 26/09/2026. At whole-page
  zoom the model misreads the month as 03, which is the bug this suite guards
  against, so sample2 is the form that exercises the fallback and re-read
  paths rather than the text-layer ones.
- Small synthetic PDFs are built in-process wherever a real text layer is
  needed, so the text-layer behaviour is pinned by a fixture that cannot be
  invalidated by swapping the sample file again.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault(
    "GOOGLE_SA_JSON_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_account.json"),
)

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import fitz

import gemini_utils as g

SAMPLE_PDF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample2.pdf"
)

# sample2.pdf's true readings, established by six independent close-up reads.
EXPECTED_START = "2026-09-25"
EXPECTED_END = "2026-09-26"

_failures = []


def check(condition, message):
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        _failures.append(message)


def sample_bytes():
    with open(SAMPLE_PDF, "rb") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# The reported bug: a 4 day survey span was treated as impossible and the
# correctly-read start date was deleted.
# ---------------------------------------------------------------------------
def test_span_does_not_invalidate_dates():
    print("\nspan of 4 days is a valid survey, not an error")
    check(
        g._dates_consistent("2026-09-21", "2026-09-25") is True,
        "sample.pdf's 4 day pair is consistent",
    )
    check(
        g._span_is_plausible("2026-09-21", "2026-09-25") is True,
        "sample.pdf's 4 day span is plausible",
    )
    check(
        g._has_complete_survey_dates(
            {"start_date": "2026-09-21", "end_date": "2026-09-25"}
        )
        is True,
        "sample.pdf's pair counts as a complete extraction",
    )


def test_only_impossible_ordering_is_rejected():
    print("\nonly an end date before the start date is impossible")
    check(
        g._dates_consistent("2026-09-25", "2026-09-21") is False,
        "inverted pair is inconsistent",
    )
    check(
        g._dates_consistent(None, "2026-09-21") is True,
        "missing start date is not an ordering error",
    )
    check(
        g._dates_consistent("2026-09-21", None) is True,
        "missing end date is not an ordering error",
    )
    check(
        g._span_is_plausible("2026-09-21", "2025-09-21") is False,
        "a negative span is implausible",
    )
    check(
        g._span_is_plausible("2015-09-21", "2026-09-21") is False,
        "an 11 year span is implausible",
    )


def test_repair_keeps_dates_both_passes_agree_on():
    print("\nregression: an agreed re-read must not delete a date")
    fields = {
        "start_date": "2026-09-21",
        "start_confidence": 0.95,
        "end_date": "2026-09-25",
        "end_confidence": 0.95,
    }
    re_read = {
        "start_date": "2026-09-21",
        "start_confidence": 0.95,
        "end_date": "2026-09-25",
        "end_confidence": 0.95,
    }
    result = run_repair(fields, re_read)
    check(
        result["start_date"] == "2026-09-21",
        "start date survives an agreeing re-read",
    )
    check(
        result["end_date"] == "2026-09-25",
        "end date survives an agreeing re-read",
    )


def test_repair_adopts_a_corrected_reading():
    print("\na re-read that disagrees is a real correction")
    fields = {
        "start_date": "2026-09-21",
        "start_confidence": 0.6,
        "end_date": "2026-09-25",
        "end_confidence": 0.9,
    }
    re_read = {
        "start_date": "2026-09-24",
        "start_confidence": 0.9,
        "end_date": "2026-09-25",
        "end_confidence": 0.9,
    }
    result = run_repair(fields, re_read)
    check(
        result["start_date"] == "2026-09-24",
        "corrected start date is adopted",
    )
    check(result["end_date"] == "2026-09-25", "unchanged end date is kept")


def test_repair_fills_a_missing_date():
    print("\na missing date is taken from the re-read")
    fields = {
        "start_date": None,
        "start_confidence": None,
        "end_date": "2026-09-25",
        "end_confidence": 0.9,
    }
    re_read = {
        "start_date": "2026-09-21",
        "start_confidence": 0.9,
        "end_date": "2026-09-25",
        "end_confidence": 0.9,
    }
    result = run_repair(fields, re_read)
    check(result["start_date"] == "2026-09-21", "missing start date is recovered")


def test_repair_keeps_both_when_inverted_and_tied():
    print("\nregression: an inverted tied pair must not always drop the start")
    fields = {
        "start_date": "2026-09-25",
        "start_confidence": 0.9,
        "end_date": "2026-09-21",
        "end_confidence": 0.9,
    }
    result = run_repair(dict(fields), dict(fields))
    check(
        result["start_date"] == "2026-09-25" and result["end_date"] == "2026-09-21",
        "both dates kept for review instead of one being discarded",
    )


def test_repair_drops_only_a_shaky_out_of_order_date():
    print("\nonly an unconfident out-of-order date is dropped")
    fields = {
        "start_date": "2026-09-25",
        "start_confidence": 0.9,
        "end_date": "2026-09-21",
        "end_confidence": 0.2,
    }
    result = run_repair(dict(fields), dict(fields))
    check(result["end_date"] is None, "the low-confidence date is dropped")
    check(result["start_date"] == "2026-09-25", "the confident date is kept")


def test_repair_keeps_both_confident_out_of_order_dates():
    print("\ntwo confident out-of-order dates are both kept for review")
    # Out of order, but neither reading is shaky enough to throw away: the
    # weaker of the two is still 0.6, so discarding it would be guessing.
    fields = {
        "start_date": "2026-09-25",
        "start_confidence": 0.6,
        "end_date": "2026-09-21",
        "end_confidence": 0.9,
    }
    result = run_repair(dict(fields), dict(fields))
    check(
        result["start_date"] == "2026-09-25" and result["end_date"] == "2026-09-21",
        "the weaker date is kept because it was still read confidently",
    )


class FakeResponse:
    """Minimal stand-in for a generate_content response.

    ``_parse_gemini_response`` reads the ``parsed`` attribute, which is where
    the SDK puts the schema-validated object.
    """

    def __init__(self, parsed):
        self.parsed = parsed


def run_repair(fields, re_read, rejected=None):
    """Drive _repair_inconsistent_dates with the model call stubbed out."""
    original = g._safe_generate
    original_zoom = g._located_date_zoom_views
    g._safe_generate = lambda contents, config, model_id=g.MODEL_ID: FakeResponse(re_read)
    # No locate call in unit tests: it is a network call and it has its own tests.
    g._located_date_zoom_views = lambda pdf_bytes: []
    try:
        return g._repair_inconsistent_dates(
            fields, [b"page"], [b"crop"], {}, rejected or {}, object(), b""
        )
    finally:
        g._safe_generate = original
        g._located_date_zoom_views = original_zoom


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------
def test_overlapping_bands_merge_into_one_crop():
    print("\noverlapping label bands collapse to a single crop")
    merged = g._merge_overlapping_rects(
        [fitz.Rect(0, 0, 10, 10), fitz.Rect(2, 2, 12, 12), fitz.Rect(40, 40, 50, 50)]
    )
    check(len(merged) == 2, "overlapping rects merged, disjoint one kept")
    check(
        (round(merged[0].x0), round(merged[0].y0), round(merged[0].x1), round(merged[0].y1))
        == (0, 0, 12, 12),
        "merged rect is the union of both",
    )
    check(g._merge_overlapping_rects([]) == [], "empty input is handled")


def test_scanned_form_still_gets_a_full_page_date_view():
    print("\nscanned form with no text layer still gets a whole-page view")
    document = fitz.open(stream=sample_bytes(), filetype="pdf")
    page = document[0]
    # sample2.pdf is a pure scan, so there is no printed label to search for and
    # the anchored path correctly finds nothing.
    check(
        g._label_anchored_date_clips(page) == [],
        "no anchored clip is invented for a form with no text layer",
    )
    views = g._page_date_views(page)
    check(
        len(views) >= 1,
        f"the whole page is still sent so the date section cannot be missed (got {len(views)})",
    )
    check(
        all(isinstance(view, bytes) and view for view in views),
        "every view is non-empty PNG bytes",
    )


def _png_size(data):
    """(width, height) of a PNG, read from its IHDR chunk."""
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def test_full_page_view_cannot_be_cropped_away():
    print("\nthe whole page is one of the date views, not a fixed band")
    document = fitz.open(stream=sample_bytes(), filetype="pdf")
    page = document[0]
    views = g._page_date_views(page)
    # The requirement is that the date section can never be cropped away on an
    # unusual layout, so the fallback must be the entire page rect. Asserted on
    # the rendered pixel size, which cannot be faked by a tighter clip.
    expected_w = int(page.rect.width * g.DATE_PAGE_ZOOM)
    expected_h = int(page.rect.height * g.DATE_PAGE_ZOOM)
    sizes = [_png_size(view) for view in views]
    check(
        (expected_w, expected_h) in sizes or any(
            abs(w - expected_w) <= 2 and abs(h - expected_h) <= 2 for w, h in sizes
        ),
        f"a view covers the whole page at DATE_PAGE_ZOOM (expected ~{expected_w}x{expected_h}, got {sizes})",
    )
    check(
        g.DATE_PAGE_ZOOM >= 2,
        f"DATE_PAGE_ZOOM is high enough to read the digits (got {g.DATE_PAGE_ZOOM})",
    )
    check(
        g.DATE_FIELD_ZOOM > g.DATE_PAGE_ZOOM,
        f"the locate-then-zoom crop is tighter than the page view ({g.DATE_FIELD_ZOOM} > {g.DATE_PAGE_ZOOM})",
    )


# ---------------------------------------------------------------------------
# Locate-then-zoom
# ---------------------------------------------------------------------------
def test_locate_coordinate_scales():
    print("\nlocate coordinates normalise from either 0-1 or 0-1000")
    # Gemini reports grounding coordinates on a native 0-1000 scale, and does
    # so in practice: sample2.pdf came back as start_date_box [177, 880].
    for raw, expected in (
        (0.5, 0.5),
        (1.0, 1.0),
        (0.0, 0.0),
        (177, 0.177),
        (880, 0.880),
        (1000, 1.0),
        (5, 0.005),
    ):
        check(
            g._normalize_locate_coordinate(raw) == expected,
            f"_normalize_locate_coordinate({raw!r}) == {expected}",
        )
    for bad in (-1, 1001, "x", None, float("nan")):
        check(
            g._normalize_locate_coordinate(bad) is None,
            f"_normalize_locate_coordinate({bad!r}) is rejected",
        )


def test_locate_box_to_rect_uses_named_components():
    print("\na located box becomes a clip on the page")
    document = fitz.open(stream=sample_bytes(), filetype="pdf")
    page = document[0]
    # Named components, so a top-left field is not transposed by a guess.
    top_left = g._fractions_to_rect(page, {"y_frac": 0.2, "x_frac": 0.15})
    check(
        top_left is not None
        and top_left.y0 < top_left.y1
        and top_left.x0 < top_left.x1,
        f"a named box yields a non-degenerate rect (got {top_left})",
    )
    if top_left is not None:
        centre_y = (top_left.y0 + top_left.y1) / 2 / page.rect.height
        centre_x = (top_left.x0 + top_left.x1) / 2 / page.rect.width
        check(
            abs(centre_y - 0.2) < 0.03 and abs(centre_x - 0.15) < 0.03,
            f"a top-left box keeps its orientation (centre {centre_y:.3f}, {centre_x:.3f})",
        )
    check(
        g._fractions_to_rect(page, [0.2, 0.15]) is not None,
        "a positional [y, x] pair is still accepted",
    )
    for bad in (None, {}, [0.2], "top-left", [2000, 0.2], [0.2, 0.2, 0.3]):
        check(g._fractions_to_rect(page, bad) is None, f"bad box {bad!r} is rejected")


def test_locate_schema_is_not_the_survey_schema():
    print("\nthe locate call uses its own response schema")
    check(
        g.DateFieldRegions is not g.SurveyFormFields,
        "locate has a distinct schema, so the reply can carry boxes",
    )
    check(
        hasattr(g.DateFieldRegions(), "start_date_box")
        and hasattr(g.DateFieldRegions(), "end_date_box"),
        "the locate schema has a box per date field",
    )


def test_located_zoom_views_survive_a_broken_locate():
    print("\na failed locate degrades to no crops instead of raising")
    original = g._locate_date_field_rects
    try:
        g._locate_date_field_rects = lambda page: []
        check(g._located_date_zoom_views(sample_bytes()) == [], "empty locate yields no crops")

        def boom(page):
            raise RuntimeError("model unavailable")

        g._locate_date_field_rects = boom
        check(
            g._located_date_zoom_views(sample_bytes()) == [],
            "a raising locate is swallowed, extraction continues",
        )
        check(g._located_date_zoom_views(b"") == [], "missing pdf bytes yield no crops")
    finally:
        g._locate_date_field_rects = original


# ---------------------------------------------------------------------------
# Text layer
# ---------------------------------------------------------------------------
def test_text_layer_ignores_ocr_garbage():
    print("\nOCR garbage in the text layer cannot override the vision reading")
    # Pinned on a fixture rather than on a sample file, because this is about
    # the parser and not about any particular form.
    found = g._extract_dates_from_pdf_text(make_text_pdf("Survey End Date: 25|01 202"))
    check(found == {}, f"no date invented from '25|01 202' (got {found})")
    # sample2.pdf is a pure scan, so it contributes no text dates at all.
    check(
        g._extract_dates_from_pdf_text(sample_bytes()) == {},
        "a scanned form with no text layer yields no text dates",
    )


def test_text_layer_reads_a_printed_date():
    print("\na genuinely machine-printed date is still picked up")
    found = g._extract_dates_from_pdf_text(make_text_pdf("Survey Start Date: 01/07/2026"))
    check(found.get("start_date") == "2026-07-01", f"start date read from text (got {found})")

    found = g._extract_dates_from_pdf_text(make_text_pdf("Survey End Date: 05/07/2026"))
    check(found.get("end_date") == "2026-07-05", f"end date read from text (got {found})")


def test_text_layer_does_not_reach_into_a_neighbouring_field():
    print("\nthe text layer does not borrow a date from an adjacent field")
    pdf = make_text_pdf("Survey Start Date:\nDetails\nProject ref 12/03/2024")
    found = g._extract_dates_from_pdf_text(pdf)
    check(
        found.get("start_date") != "2024-03-12",
        f"an out-of-range date from a neighbouring field is rejected (got {found})",
    )


def make_text_pdf(body):
    document = fitz.open()
    page = document.new_page(width=576, height=792)
    page.insert_text((60, 120), body, fontsize=11)
    return document.tobytes()


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------
def test_valid_date_normalisation():
    print("\ndate normalisation still handles the form's real formats")
    for raw, expected in (
        ("21/09/2026", "2026-09-21"),
        ("25-09-2026", "2026-09-25"),
        ("2026-09-21", "2026-09-21"),
        ("21-09-26", "2026-09-21"),
        ("21/9/26", "2026-09-21"),
        ("21-09-026", "2026-09-21"),
        ("4 September 2026", "2026-09-04"),
        ("21st September 2026", "2026-09-21"),
        ("25|01 202", None),
        ("21-09-2025", None),
        (None, None),
        ("", None),
    ):
        check(g._valid_date(raw) == expected, f"_valid_date({raw!r}) == {expected!r}")


# ---------------------------------------------------------------------------
# Parsing is separate from the project window
# ---------------------------------------------------------------------------
def test_parse_is_independent_of_the_window():
    print("\nparsing and window-validation are separable")
    # The whole point of the split: a misread digit must stay recoverable even
    # though the resulting date is not storable.
    check(
        g._parse_date("25/03/2026") == "2026-03-25",
        "an out-of-window but real date still parses",
    )
    check(
        g._valid_date("25/03/2026") is None,
        "but it is not offered up for storage",
    )
    for raw in ("25|01 202", "not a date", None, ""):
        check(g._parse_date(raw) is None, f"_parse_date({raw!r}) is None")


def test_rejected_reading_survives_as_a_hint():
    print("\na rejected reading is preserved for the re-read")
    finding = g._rejected_reading("25/03/2026")
    check(finding == ("25/03/2026", "2026-03-25"), f"raw and parsed are kept (got {finding})")
    # Nothing to say about a blank, an unparseable value, or a value that was
    # already acceptable.
    for raw in (None, "", "25|01 202", EXPECTED_START, "2026-07-01"):
        check(
            g._rejected_reading(raw) is None,
            f"no hint for {raw!r}, which needs no repair",
        )


def test_future_dates_get_a_grace_period():
    print("\na date a few days ahead is tolerated, a distant one is not")
    from datetime import date as _date, timedelta as _timedelta

    today = _date.today()
    soon = (today + _timedelta(days=g.DATE_FUTURE_GRACE_DAYS)).strftime("%Y-%m-%d")
    far = (today + _timedelta(days=365)).strftime("%Y-%m-%d")
    check(
        g._valid_date(soon) == soon,
        f"a date {g.DATE_FUTURE_GRACE_DAYS} days ahead is accepted (got {g._valid_date(soon)!r})",
    )
    check(g._valid_date(far) is None, "a date a year ahead is still rejected")
    check(
        g._valid_date(today.strftime("%Y-%m-%d")) == today.strftime("%Y-%m-%d"),
        "today is accepted",
    )
    # The floor is inclusive: the project's own start date is valid, the day
    # before it is the misread this window exists to catch.
    check(
        g._valid_date(g.MIN_SURVEY_DATE.strftime("%Y-%m-%d")) is not None,
        "the project start date itself is accepted",
    )
    just_before = (g.MIN_SURVEY_DATE - _timedelta(days=1)).strftime("%Y-%m-%d")
    check(
        g._valid_date(just_before) is None,
        f"the day before the project start is rejected (got {g._valid_date(just_before)!r})",
    )


def test_rejected_hint_reaches_the_repair_prompt():
    print("\nthe re-read is told what it read and why it was dropped")
    captured = {}

    def fake_generate(contents, config, model_id=g.MODEL_ID):
        # The prompt is appended as a bare string, not a Part, so read both.
        captured["prompt"] = "\n".join(
            part if isinstance(part, str) else (getattr(part, "text", "") or "")
            for part in contents
        )
        return FakeResponse(
            {
                "start_date": "2026-09-25",
                "start_confidence": 0.9,
                "end_date": "2026-09-26",
                "end_confidence": 0.9,
            }
        )

    fields = {
        "start_date": None,
        "start_confidence": None,
        "end_date": None,
        "end_confidence": None,
        "ae_ie_sc_name": None,
        "piu_name": None,
        "contractor_agency": None,
    }
    original = g._safe_generate
    original_zoom = g._located_date_zoom_views
    try:
        g._safe_generate = fake_generate
        g._located_date_zoom_views = lambda pdf_bytes: []
        g._repair_inconsistent_dates(
            fields,
            [b"page"],
            [b"view"],
            {},
            {"start_date": ("25/03/2026", "2026-03-25")},
            SimpleNamespace(),
            b"",
        )
    finally:
        g._safe_generate = original
        g._located_date_zoom_views = original_zoom

    prompt = captured.get("prompt", "")
    check("25/03/2026" in prompt, "the re-read prompt names the reading it rejected")
    check("2026-03-25" in prompt, "and the date it resolved to")
    check("rejected" in prompt.lower(), "and says plainly that it was rejected")
    check(
        fields.get("start_date") == EXPECTED_START and fields.get("end_date") == EXPECTED_END,
        f"the repaired fields are the true dates (got {fields})",
    )


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
