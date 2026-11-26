"""Record when the RoadVision raw-video and defect-report files arrived.

The portal pulls both files from the RoadVision API and the API cannot push,
so the portal learns that a file has landed by reading the inventory.  The
inventory entry carries the upstream's own timestamp for the file, and that is
what gets recorded - the time RoadVision says the file was written.  When an
entry has no usable timestamp the moment the portal noticed the file is
recorded instead, which is later but never wrong by much.

Both columns are nullable, and a survey without a stamp simply has not been
seen yet.

Nothing here sends anything: it only records what is already there.
"""

import logging
import re
from datetime import datetime, timezone

from extensions import db
from models.db_models import Survey
from utils.defect_report_api import DefectReportUnavailable, list_defect_report_files
from utils.raw_xlsx import (
    RawXlsxUnavailable,
    list_raw_xlsx_files,
    normalise_dn,
    normalise_label,
    stretch_labels,
)


log = logging.getLogger(__name__)


def _normalised_label(value):
    return normalise_label(value)


def build_inventory_index(files, by_shift=True):
    """``(label, cycle[, dn])`` -> the inventory entry, ready to look up.

    With ``by_shift`` the key includes ``dn``, normalised the same way for both
    inventories: ``"night"`` for a night survey, ``""`` for a day one.  Without
    it the key is only the stretch and cycle, which is what the raw-XLSX
    inventory needs because it does not mark night files.
    """
    index = {}
    for item in files or []:
        if not isinstance(item, dict):
            continue
        label = _normalised_label(item.get("label"))
        try:
            cycle = int(item.get("cycle"))
        except (TypeError, ValueError):
            continue
        if not label:
            continue
        if by_shift:
            index[(label, cycle, normalise_dn(item.get("dn")))] = item
        else:
            index[(label, cycle)] = item
    return index


def survey_inventory_keys(section_no, upc_code, cycle_no, survey_type, by_shift=True):
    """The index keys a survey could be stored under.

    A stretch is written in several ways on the two sides ("N/09086/01002/UP"
    here, "N0908601002UP" there), and a section can name more than one
    stretch, so every plausible key is returned.  ``by_shift`` matches
    :func:`build_inventory_index`.
    """
    if cycle_no is None:
        return []

    labels = stretch_labels(section_no, upc_code)

    if not by_shift:
        return [(label, int(cycle_no)) for label in labels]

    dn = "night" if str(survey_type or "").strip().casefold() == "night" else ""
    return [(label, int(cycle_no), dn) for label in labels]


def item_received_at(item):
    """The upstream's own timestamp for a file, as a naive UTC datetime.

    RoadVision reports when it last wrote the file, so this is the moment
    the file arrived.  ``None`` when the entry carries no usable timestamp.
    """
    raw = str((item or {}).get("updated") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _received_value(current, item, now):
    """The received time to keep for a file.

    A file that has never been stamped gets its upstream time (or ``now`` when
    there is none). A stamped file moves forward when RoadVision has written
    it again, so the date follows the file's latest update.
    """
    stamp = item_received_at(item)
    if current is None:
        return stamp or now
    if stamp and stamp > current:
        return stamp
    return current


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_inventories():
    """Read both RoadVision inventories once.

    ``None`` means the inventory could not be read (the host is down or the
    key is missing); an empty list means it was read and it is empty.  The
    two are not the same thing, so they are not collapsed.
    """
    inventories = {"raw": None, "defect": None}

    try:
        inventories["raw"] = list_raw_xlsx_files()
    except (RawXlsxUnavailable, RuntimeError) as exc:
        log.info("raw-video inventory unavailable: %s", exc)

    try:
        inventories["defect"] = list_defect_report_files()
    except (DefectReportUnavailable, RuntimeError) as exc:
        log.info("defect-report inventory unavailable: %s", exc)

    return inventories


def record_received_timestamps(inventories=None, limit=2000):
    """Stamp every survey whose RoadVision file has arrived.

    Returns a small summary.  Safe to call often: only surveys without a
    stamp are touched, and a file that is not in the inventory changes
    nothing.
    """
    summary = {
        "raw_video_stamped": 0,
        "defect_report_stamped": 0,
        "raw_inventory": False,
        "defect_inventory": False,
    }

    if inventories is None:
        inventories = fetch_inventories()

    raw_files = inventories.get("raw")
    defect_files = inventories.get("defect")
    summary["raw_inventory"] = raw_files is not None
    summary["defect_inventory"] = defect_files is not None

    raw_index = build_inventory_index(raw_files, by_shift=False)
    defect_index = build_inventory_index(defect_files, by_shift=False)

    if not raw_index and not defect_index:
        return summary

    surveys = (
        Survey.query.filter(
            Survey.end_survey_pdf.isnot(None),
            (Survey.status.is_(None)) | (Survey.status != "cancelled"),
        )
        .limit(limit)
        .all()
    )

    now = _now()
    changed = False

    for survey in surveys:
        if raw_index:
            raw_keys = survey_inventory_keys(
                survey.section_no,
                survey.upc_code,
                survey.cycle_no,
                survey.survey_type,
                by_shift=False,
            )
            match = next((raw_index[key] for key in raw_keys if key in raw_index), None)
            if match is not None:
                new_value = _received_value(survey.raw_video_received_at, match, now)
                if new_value != survey.raw_video_received_at:
                    key = "raw_video_stamped" if survey.raw_video_received_at is None else "raw_video_restamped"
                    summary[key] = summary.get(key, 0) + 1
                    survey.raw_video_received_at = new_value
                    changed = True

        if defect_index:
            keys = survey_inventory_keys(
                survey.section_no,
                survey.upc_code,
                survey.cycle_no,
                survey.survey_type,
                by_shift=False,
            )
            match = next(
                (defect_index[key] for key in keys if key in defect_index),
                None,
            )
            if match is not None:
                new_value = _received_value(survey.defect_report_received_at, match, now)
                if new_value != survey.defect_report_received_at:
                    key = "defect_report_stamped" if survey.defect_report_received_at is None else "defect_report_restamped"
                    summary[key] = summary.get(key, 0) + 1
                    survey.defect_report_received_at = new_value
                    changed = True

    if changed:
        db.session.commit()

    return summary
