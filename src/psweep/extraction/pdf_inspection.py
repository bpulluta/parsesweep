"""Deterministic PDF revision evidence, not a determination of legal status.

Only native PDF objects are inspected: no OCR, rendering, or body-keyword scan.
A negative result means no supported strong signal was found, not that a document
is final. Geometric thresholds are deliberately conservative detector constants,
versioned with the implementation; draft wording comes only from supplied regexes.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET  # noqa: S405 -- bounded XML, DTDs/entities rejected before parsing.
from dataclasses import dataclass
from pathlib import Path

from psweep.utils.file_io import artifact_sha256

INSPECTOR_VERSION = "1"
_EVIDENCE_LIMIT = 50
_TEXT_LIMIT = 200
_HEADER_CHAR_LIMIT = 100
_MIN_IMAGE_TEXT_CHARS = 100
_MAX_XMP_CHARS = 1_000_000
_PARSE_ERRORS = (OSError, RuntimeError, ValueError)


@dataclass(frozen=True)
class _TextLine:
    text: str
    bbox: tuple[float, float, float, float]
    glyphs: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True)
class _Mark:
    x0: float
    x1: float
    y: float
    thickness: float
    kind: str


def _limitation(result: dict, code: str, *, coverage: bool = True) -> None:
    if code not in result["limitations"]:
        result["limitations"].append(code)
    if coverage and result["state"] != "flagged":
        result["state"] = "unknown"


def _evidence(result: dict, kind: str, page: int | None, *, weak=False, **details) -> None:
    item = {"kind": kind, "strength": "weak" if weak else "strong", "page": page, **details}
    if "text" in item:
        item["text"] = " ".join(item["text"].split())[:_TEXT_LIMIT]
    if "bbox" in item:
        item["bbox"] = [round(float(value), 2) for value in item["bbox"]]
        if not all(math.isfinite(value) for value in item["bbox"]):
            item.pop("bbox")
    result["evidence_count"] += 1
    if not weak:
        result["state"] = "flagged"
    if len(result["evidence"]) < _EVIDENCE_LIMIT:
        result["evidence"].append(item)
        return
    _limitation(result, "evidence_truncated", coverage=False)
    # Never let earlier weak highlights consume the whole positive-evidence budget.
    if not weak:
        for index, previous in enumerate(result["evidence"]):
            if previous["strength"] == "weak":
                result["evidence"].pop(index)
                result["evidence"].append(item)
                break


def _matches(text: str, patterns: list[re.Pattern]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _metadata(document, patterns: list[re.Pattern], result: dict) -> None:
    if not patterns:
        return
    for field in ("title", "subject", "keywords"):
        text = (document.metadata or {}).get(field) or ""
        if _matches(text, patterns):
            _evidence(result, "draft_metadata", None, field=field, text=text)
    xmp = document.get_xml_metadata()
    if not xmp:
        return
    # XML metadata is untrusted; do not resolve declarations or unbounded XML.
    if len(xmp) > _MAX_XMP_CHARS or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xmp, re.IGNORECASE):
        _limitation(result, "unreadable_xmp")
        return
    try:
        root = ET.fromstring(xmp)  # noqa: S314 -- DTDs/entities rejected above.
    except ET.ParseError:
        _limitation(result, "unreadable_xmp")
        return
    fields = {
        "{http://purl.org/dc/elements/1.1/}title": "title",
        "{http://purl.org/dc/elements/1.1/}description": "subject",
        "{http://purl.org/dc/elements/1.1/}subject": "keywords",
        "{http://ns.adobe.com/pdf/1.3/}Keywords": "keywords",
    }
    for node in root.iter():
        candidates = list(node.attrib.items())
        if node.tag in fields:
            candidates.append((node.tag, " ".join(node.itertext())))
        for tag, text in candidates:
            if tag in fields and _matches(text, patterns):
                _evidence(result, "draft_metadata", None, field=f"xmp:{fields[tag]}", text=text)


def _native_lines(page, result: dict, *, flags: int) -> list[_TextLine]:
    lines = []
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            characters = [char for span in line["spans"] for char in span["chars"]]
            text = "".join(char["c"] for char in characters)
            if not text.strip():
                continue
            dx, dy = line["dir"]
            if line.get("wmode", 0) != 0 or abs(dx - 1) > 0.01 or abs(dy) > 0.01:
                _limitation(result, "rotated_text")
                continue
            bbox = tuple(line["bbox"])
            glyphs = tuple(tuple(char["bbox"]) for char in characters if char["c"].strip())
            if not all(math.isfinite(value) for value in bbox) or bbox[3] <= bbox[1]:
                _limitation(result, "invalid_text_geometry")
                continue
            lines.append(_TextLine(text, bbox, glyphs))
    return lines


def _headers(
    lines: list[_TextLine],
    height: float,
    patterns: list[re.Pattern],
    result: dict,
    page_number: int,
) -> None:
    for line in lines:
        text = line.text.strip()
        if line.bbox[3] > height * 0.18 or len(text) > _HEADER_CHAR_LIMIT:
            continue
        # Full-line matches prevent ordinary sentences mentioning draft submissions
        # from being interpreted as document status. Callers may supply phrase regexes.
        if any(pattern.fullmatch(text) for pattern in patterns):
            _evidence(result, "draft_header", page_number, text=text, bbox=line.bbox)


def _annotations(page, result: dict, page_number: int) -> list[tuple]:
    regions = []
    for annotation in page.annots() or ():
        kind = annotation.type[1]
        rect = tuple(annotation.rect)
        # Avoid counting StrikeOut appearances twice. Do not suppress other
        # overlapping marks: a highlight can cover a genuine flattened strike.
        if kind == "StrikeOut":
            regions.append(rect)
        if kind in {"StrikeOut", "Highlight"}:
            _evidence(
                result,
                "strikeout_annotation" if kind == "StrikeOut" else "highlight_annotation",
                page_number,
                weak=kind == "Highlight",
                bbox=rect,
            )
    return regions


def _segments(drawings: list[dict]) -> tuple[list[_Mark], list[tuple]]:
    marks, verticals = [], []
    for drawing in drawings:
        stroked = "s" in drawing["type"] and drawing.get("stroke_opacity", 1) > 0
        filled = "f" in drawing["type"] and drawing.get("fill_opacity", 1) > 0
        for item in drawing["items"]:
            if item[0] == "l" and stroked:
                start, end = item[1:3]
                if abs(start.x - end.x) <= 0.2 and abs(start.y - end.y) > 2:
                    verticals.append((start.x, min(start.y, end.y), max(start.y, end.y)))
                if abs(start.y - end.y) <= 0.2 and abs(start.x - end.x) >= 3:
                    marks.append(
                        _Mark(
                            min(start.x, end.x),
                            max(start.x, end.x),
                            (start.y + end.y) / 2,
                            drawing.get("width", 1),
                            "text_crossing_stroke",
                        )
                    )
            elif item[0] == "re":
                rect = item[1]
                if filled and 0 < rect.height <= 2 and rect.width >= 3:
                    marks.append(
                        _Mark(
                            rect.x0,
                            rect.x1,
                            (rect.y0 + rect.y1) / 2,
                            rect.height,
                            "text_crossing_rectangle",
                        )
                    )
                elif rect.height > 2 and (stroked or filled):
                    verticals.extend(((rect.x0, rect.y0, rect.y1), (rect.x1, rect.y0, rect.y1)))
                # Ordinary rectangular outlines are borders, not isolated strikes.
    return marks, verticals


def _crossed_line(mark: _Mark, lines: list[_TextLine]) -> _TextLine | None:
    length = mark.x1 - mark.x0
    for line in lines:
        x0, y0, x1, y1 = line.bbox
        height = y1 - y0
        # A strike lies within the glyph interior, above the baseline/underline zone.
        # 59% includes the thin filled rectangles emitted by common PDF producers.
        if not 0.35 <= (mark.y - y0) / height <= 0.68:
            continue
        if mark.thickness > min(2.0, height * 0.12):
            continue
        overlap = max(0, min(x1, mark.x1) - max(x0, mark.x0))
        if overlap / length < 0.8:
            continue
        crossed = 0
        for gx0, gy0, gx1, gy1 in line.glyphs:
            if mark.x0 <= (gx0 + gx1) / 2 <= mark.x1 and gy0 + 0.35 * (
                gy1 - gy0
            ) <= mark.y <= gy0 + 0.68 * (gy1 - gy0):
                crossed += 1
                if crossed >= 2:
                    return line
    return None


def _flattened(
    drawings: list[dict],
    lines: list[_TextLine],
    annotation_regions: list[tuple],
    result: dict,
    page_number: int,
) -> None:
    marks, verticals = _segments(drawings)
    seen = set()
    for mark in marks:
        if any(
            x0 <= mark.x0 and mark.x1 <= x1 and y0 <= mark.y <= y1
            for x0, y0, x1, y1 in annotation_regions
        ):
            continue
        if any(
            (abs(x - mark.x0) <= 1 or abs(x - mark.x1) <= 1) and y0 < mark.y < y1
            for x, y0, y1 in verticals
        ):
            continue
        line = _crossed_line(mark, lines)
        key = (round(mark.x0, 2), round(mark.x1, 2), round(mark.y, 2))
        if line is not None and key not in seen:
            seen.add(key)
            _evidence(
                result,
                mark.kind,
                page_number,
                text=line.text,
                bbox=(mark.x0, mark.y - mark.thickness / 2, mark.x1, mark.y + mark.thickness / 2),
            )


def _coverage(page, lines: list[_TextLine], drawings: list[dict], result: dict) -> None:
    native_chars = sum(len(line.text.strip()) for line in lines)
    # get_image_info returns placement metadata, not image bytes. Ignore small logos
    # on native pages. Even plentiful OCR text cannot reveal raster-only revision marks.
    for image in page.get_image_info():
        x0, y0, x1, y1 = image["bbox"]
        image_area = max(0, x1 - x0) * max(0, y1 - y0)
        large_image = image_area >= page.rect.get_area() * 0.2
        if native_chars == 0 or (large_image and native_chars < _MIN_IMAGE_TEXT_CHARS):
            _limitation(result, "insufficient_native_text")
        if large_image:
            _limitation(result, "raster_image_coverage")
    if not native_chars and drawings:
        _limitation(result, "graphics_without_native_text")


def inspect_pdf(path: Path, *, draft_patterns: list[str] | None = None) -> dict:
    """Inspect every native page for observable revision signals.

    Returns JSON-safe state, version, SHA-256, page/inspection counts, evidence
    (at most 50 entries, each text excerpt at most 200 characters), total evidence
    count, and deduplicated limitation codes. Pages and evidence use 1-based page
    numbers; metadata evidence has ``page=None``. ``inspected_pages`` counts pages
    whose native inspection completed, including image pages with limited coverage.

    ``flagged`` requires strong evidence, even when coverage is incomplete;
    ``unknown`` means incomplete inspection without strong evidence; ``no_findings``
    means no strong signal in supported objects. Highlights are weak evidence only.
    No state establishes whether an ordinance is adopted, valid, or legally final.
    Draft patterns search title/subject/keywords and full-match short header lines,
    case-insensitively, matching the selection-stage pattern semantics;
    no implicit vocabulary is used. Invalid patterns raise ValueError; unreadable,
    corrupt, password-protected, or unsupported PDFs return ``unknown`` instead.
    """
    try:
        patterns = [re.compile(pattern, re.IGNORECASE) for pattern in draft_patterns or []]
    except (re.error, TypeError) as exc:
        raise ValueError("Invalid PDF draft pattern") from exc
    result = {
        "state": "no_findings",
        "inspector_version": INSPECTOR_VERSION,
        "source_sha256": None,
        "page_count": None,
        "inspected_pages": 0,
        "evidence": [],
        "evidence_count": 0,
        "limitations": [],
    }
    try:
        result["source_sha256"] = artifact_sha256(path)
    except OSError:
        _limitation(result, "unreadable_source")
        return result
    try:
        import pymupdf
    except ImportError:
        _limitation(result, "inspector_unavailable")
        return result
    try:
        with pymupdf.open(path) as document:
            result["page_count"] = document.page_count
            if not document.is_pdf:
                _limitation(result, "not_pdf")
                return result
            if document.needs_pass or (document.metadata or {}).get("encryption"):
                _limitation(result, "encrypted_pdf")
                return result
            if document.is_repaired:
                _limitation(result, "repaired_pdf")
            if document.page_count == 0:
                _limitation(result, "empty_pdf")
            try:
                _metadata(document, patterns, result)
            except _PARSE_ERRORS:
                _limitation(result, "unreadable_metadata")
            flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            # Keep only one page's native objects; load failures cannot hide later pages.
            for number in range(1, document.page_count + 1):
                try:
                    page = document.load_page(number - 1)
                    regions = _annotations(page, result, number)
                    lines = _native_lines(page, result, flags=flags)
                    _headers(lines, page.rect.height, patterns, result, number)
                    drawings = page.get_drawings()
                    _flattened(drawings, lines, regions, result, number)
                    _coverage(page, lines, drawings, result)
                    result["inspected_pages"] += 1
                except Exception:  # noqa: BLE001 -- malformed page objects must not abort the scan.
                    _limitation(result, "unreadable_page")
    except Exception:  # noqa: BLE001 -- parser failures are an unknown result, not a pipeline error.
        _limitation(result, "unreadable_pdf")
    return result
