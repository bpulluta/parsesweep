"""LLM-assisted section targeting for non-PDF documents.

Large HTML, DOCX, and TXT documents can overflow the extraction context just
like large PDFs. This module finds the text regions that hold a described
section so extraction can target only those regions.

Strategy (mirrors PageLocator):

1. Heuristic pre-filter — split document into heading-delimited chunks,
   score each chunk by keyword hit density, keep the dense ones.
2. LLM confirm — send a compact index (heading + snippet) of candidate
   chunks and ask which ones contain the described section.

Results are cached in a ``.sections/<stem>.json`` + ``.sections/<stem>.txt``
sidecar pair next to the source file (keyed on size/mtime and section
description) so the LLM call happens once.

Everything is best-effort: on any failure ``locate`` returns ``None`` and the
caller falls back to full-document extraction.

Supported formats: HTML/HTM, DOCX/DOC, TXT.
Unsupported (always full extraction): XLSX, CSV, PDF.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .page_locator import (
    DEFAULT_PAGE_TRIGGER_CHARS,
    _BaseLocator,
)

logger = logging.getLogger(__name__)

SECTION_CACHE_DIRNAME = ".sections"

# Section marker pattern for plain-text documents.
_SECTION_MARKER_RE = re.compile(
    r"^(?:"
    r"§\s*\d[\d.]*"
    r"|Sec(?:tion)?\s+\d[\d.]*"
    r"|SECTION\s+\d[\d.]*"
    r"|ARTICLE\s+[A-Z0-9]+"
    r"|Article\s+[A-Z0-9]+"
    r"|Chapter\s+\d[\d.]*"
    r"|CHAPTER\s+\d[\d.]*"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_LOCATE_SYSTEM = (
    "You locate where a described section lives inside a document that has been "
    "split into numbered sections. Return the section numbers (1-indexed) that "
    "contain or are directly relevant to the described content. Include all "
    "relevant sections."
)

_LOCATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant_sections": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "reason": {"type": "string"},
    },
    "required": ["relevant_sections"],
}

# Paragraph chunk target size when splitting plain-text by blank lines.
_TXT_CHUNK_CHARS = 2_000


@dataclass
class _Chunk:
    """A logical section of a document with an optional heading and body text."""

    heading: str
    text: str

    def to_text(self) -> str:
        if self.heading:
            return f"{self.heading}\n{self.text}"
        return self.text


def _table_to_markdown(table) -> str:
    """Convert a BeautifulSoup <table> element to pipe-delimited markdown rows."""
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


class SectionLocator(_BaseLocator):
    """Find the text regions holding a described section in a large non-PDF document."""

    def __init__(
        self,
        section_description: str,
        *,
        model: str | None = None,
        models: dict[str, str] | None = None,
        default_model: str | None = None,
        trigger_chars: int = DEFAULT_PAGE_TRIGGER_CHARS,
        keywords: list[str] | None = None,
        snippet_chars: int = 600,
    ) -> None:
        super().__init__(
            section_description,
            model=model,
            models=models,
            default_model=default_model,
            trigger_chars=trigger_chars,
            keywords=keywords,
            snippet_chars=snippet_chars,
        )

    # -- keyword heuristics ------------------------------------------------

    def _score_chunk(self, chunk: _Chunk) -> int:
        low = (chunk.heading + " " + chunk.text).lower()
        return sum(low.count(kw) for kw in self._keywords)

    def _candidate_chunks(self, chunks: list[_Chunk]) -> list[int]:
        """Return 0-indexed candidate chunk indices by keyword density."""
        scores = [self._score_chunk(c) for c in chunks]
        if not any(scores):
            return []
        threshold = max(1, int(0.4 * max(scores)))
        return [i for i, s in enumerate(scores) if s >= threshold]

    # -- document splitting ------------------------------------------------

    def _split_html_sections(self, html_text: str) -> list[_Chunk]:
        """Split HTML into heading-delimited chunks; tables become markdown."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.debug("bs4 not available; HTML section splitting skipped")
            return [_Chunk("", html_text)]

        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "noscript"]):
            tag.decompose()

        _BREAK = "\x00BREAK\x00"
        HEADING_TAGS = {"h1", "h2", "h3", "h4"}

        # Replace tables with markdown in-place before heading processing.
        for table in list(soup.find_all("table")):
            md = _table_to_markdown(table)
            if md:
                table.replace_with(soup.new_string(f"\n{md}\n"))
            else:
                table.decompose()

        # Replace heading elements with sentinel-prefixed text.
        for hx in list(soup.find_all(list(HEADING_TAGS))):
            heading_text = hx.get_text(" ", strip=True)
            hx.replace_with(soup.new_string(f"\n{_BREAK}{heading_text}\n"))

        full_text = (soup.body or soup).get_text("\n")
        parts = full_text.split(_BREAK)

        chunks: list[_Chunk] = []
        for i, part in enumerate(parts):
            if i == 0:
                text = part.strip()
                if text:
                    chunks.append(_Chunk("", text))
            else:
                first_nl = part.find("\n")
                if first_nl == -1:
                    heading = part.strip()
                    text = ""
                else:
                    heading = part[:first_nl].strip()
                    text = part[first_nl:].strip()
                if heading or text:
                    chunks.append(_Chunk(heading, text))

        return [c for c in chunks if c.text.strip()]

    def _split_docx_sections(self, docx_path: Path) -> list[_Chunk]:
        """Split DOCX into heading-delimited chunks; tables become markdown rows."""
        try:
            import docx as _docx  # lazy import for optional dep
        except ImportError:
            logger.debug("python-docx not available; DOCX section splitting skipped")
            return []

        try:
            doc = _docx.Document(str(docx_path))
        except Exception as exc:
            logger.debug(f"Failed to open DOCX {docx_path.name}: {exc}")
            return []

        _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        def _local(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        def _para_text(elem) -> str:
            return "".join(t.text or "" for t in elem.iter(f"{{{_W}}}t"))

        def _para_style(elem) -> str:
            pPr = elem.find(f"{{{_W}}}pPr")
            if pPr is None:
                return ""
            pStyle = pPr.find(f"{{{_W}}}pStyle")
            if pStyle is None:
                return ""
            return pStyle.get(f"{{{_W}}}val", "")

        def _table_md(tbl_elem) -> str:
            rows: list[str] = []
            for tr in tbl_elem.iter(f"{{{_W}}}tr"):
                cells = [
                    "".join(t.text or "" for t in tc.iter(f"{{{_W}}}t")).strip()
                    for tc in tr.findall(f"{{{_W}}}tc")
                ]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            return "\n".join(rows)

        chunks: list[_Chunk] = []
        cur_heading = ""
        cur_parts: list[str] = []

        for child in doc.element.body:
            ln = _local(child.tag)
            if ln == "p":
                style = _para_style(child).lower()
                text = _para_text(child).strip()
                if style.startswith("heading") and text:
                    if cur_parts:
                        chunks.append(_Chunk(cur_heading, "\n".join(cur_parts)))
                    cur_heading = text
                    cur_parts = []
                elif text:
                    cur_parts.append(text)
            elif ln == "tbl":
                md = _table_md(child)
                if md:
                    cur_parts.append(md)

        if cur_parts:
            chunks.append(_Chunk(cur_heading, "\n".join(cur_parts)))

        return [c for c in chunks if c.text.strip()]

    def _split_txt_sections(self, text: str) -> list[_Chunk]:
        """Split plain text by section markers or blank-line paragraphs."""
        # Try explicit section markers first.
        marker_positions = [m.start() for m in _SECTION_MARKER_RE.finditer(text)]
        if len(marker_positions) > 1:
            chunks: list[_Chunk] = []
            # Preamble before first marker
            preamble = text[: marker_positions[0]].strip()
            if preamble:
                chunks.append(_Chunk("", preamble))
            for i, pos in enumerate(marker_positions):
                end = marker_positions[i + 1] if i + 1 < len(marker_positions) else len(text)
                block = text[pos:end]
                first_nl = block.find("\n")
                if first_nl == -1:
                    heading = block.strip()
                    body = ""
                else:
                    heading = block[:first_nl].strip()
                    body = block[first_nl:].strip()
                if heading or body:
                    chunks.append(_Chunk(heading, body))
            return [c for c in chunks if c.text.strip()]

        # Fall back: split by blank lines and group into ~_TXT_CHUNK_CHARS chunks.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paragraphs) <= 1:
            return [_Chunk("", text.strip())] if text.strip() else []

        chunks = []
        cur_parts: list[str] = []
        cur_len = 0
        section_num = 0
        for para in paragraphs:
            cur_parts.append(para)
            cur_len += len(para)
            if cur_len >= _TXT_CHUNK_CHARS:
                section_num += 1
                chunks.append(_Chunk(f"Part {section_num}", "\n\n".join(cur_parts)))
                cur_parts = []
                cur_len = 0
        if cur_parts:
            section_num += 1
            chunks.append(_Chunk(f"Part {section_num}", "\n\n".join(cur_parts)))
        return chunks

    # -- LLM confirmation --------------------------------------------------

    def _confirm_with_llm(
        self, chunks: list[_Chunk], candidates: list[int]
    ) -> list[int] | None:
        """Ask the LLM which candidate section indices (0-indexed) are relevant."""
        index_lines = []
        for idx in candidates:
            chunk = chunks[idx]
            heading = chunk.heading or f"Section {idx + 1}"
            snippet = " ".join(chunk.text.split())[: self._snippet_chars]
            index_lines.append(f"[section {idx + 1}] {heading}: {snippet}")
        index_text = "\n\n".join(index_lines)

        user_prompt = (
            f"TARGET SECTION: {self._description}\n\n"
            "Below are excerpts from candidate sections of a document (section "
            "numbers are 1-indexed). Return the section numbers that contain "
            "the target content as JSON "
            "{\"relevant_sections\": [N, M, ...]}.\n\n"
            f"CANDIDATE SECTIONS:\n{index_text}"
        )
        try:
            result = self._ensure_client().extract(
                text=index_text,
                schema=_LOCATE_SCHEMA,
                system_prompt=_LOCATE_SYSTEM,
                user_prompt=user_prompt,
                suppress_errors=True,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to no targeting
            logger.debug(f"Section locator LLM call failed: {exc}")
            return None

        data = result.get("data") if isinstance(result, dict) else None
        section_nums = (data or {}).get("relevant_sections") or []
        result_indices: list[int] = []
        for n in section_nums:
            try:
                idx = int(n) - 1
                if 0 <= idx < len(chunks):
                    result_indices.append(idx)
            except (TypeError, ValueError):
                continue
        return result_indices if result_indices else None

    # -- cache -------------------------------------------------------------

    @staticmethod
    def _cache_paths(file_path: Path) -> tuple[Path, Path]:
        """Return (meta_path, text_path) for the section cache sidecar."""
        cache_dir = file_path.parent / SECTION_CACHE_DIRNAME
        return (
            cache_dir / f"{file_path.stem}.json",
            cache_dir / f"{file_path.stem}.txt",
        )

    def _read_cache(self, file_path: Path) -> str | None:
        meta_path, text_path = self._cache_paths(file_path)
        if not meta_path.exists() or not text_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stat = file_path.stat()
            if int(meta.get("source_size", -1)) != stat.st_size:
                return None
            if int(meta.get("source_mtime_ns", -1)) != stat.st_mtime_ns:
                return None
            if meta.get("section_hash") != self._section_hash():
                return None
            return text_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 - a bad cache must never break extraction
            return None

    def _write_cache(self, file_path: Path, text: str) -> None:
        meta_path, text_path = self._cache_paths(file_path)
        try:
            stat = file_path.stat()
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(
                json.dumps(
                    {
                        "source_size": stat.st_size,
                        "source_mtime_ns": stat.st_mtime_ns,
                        "section_hash": self._section_hash(),
                        "section_description": self._description,
                        "method": "heuristic+llm",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            text_path.write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001 - caching is an optimization, never fatal
            pass

    # -- entry point -------------------------------------------------------

    def locate(self, file_path: str | Path) -> str | None:
        """Return filtered section text for the document, or None.

        Returns ``None`` for PDF, XLSX, CSV (unsupported or delegated), for
        documents smaller than ``trigger_chars``, and on any failure —
        signalling the caller to fall back to full-document extraction.
        """
        file_path = Path(file_path)
        ext = file_path.suffix.lower()

        # Unsupported types: delegate PDF to PageLocator; XLSX/CSV always full.
        if ext in {".pdf", ".xlsx", ".csv"}:
            return None

        if ext not in {".html", ".htm", ".docx", ".doc", ".txt"}:
            return None

        # Size check: for byte-comparable formats, use file size as proxy.
        # For text formats we compare actual char count after reading.
        try:
            file_size = file_path.stat().st_size
        except OSError:
            return None

        # DOCX is binary; use raw file size for the trigger check.
        if ext in {".docx", ".doc"} and file_size <= self.trigger_chars:
            return None

        # Check cache.
        cached = self._read_cache(file_path)
        if cached is not None:
            logger.debug(f"Section targeting cache hit for {file_path.name}")
            return cached

        # Split into chunks.
        try:
            if ext in {".html", ".htm"}:
                raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
                if len(raw_text) <= self.trigger_chars:
                    return None
                chunks = self._split_html_sections(raw_text)
            elif ext in {".docx", ".doc"}:
                chunks = self._split_docx_sections(file_path)
            else:  # .txt
                raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
                if len(raw_text) <= self.trigger_chars:
                    return None
                chunks = self._split_txt_sections(raw_text)
        except Exception as exc:  # noqa: BLE001 - fall back to full extraction
            logger.debug(f"Section splitting failed for {file_path.name}: {exc}")
            return None

        if not chunks:
            return None

        candidates = self._candidate_chunks(chunks)
        if not candidates:
            logger.debug(
                f"Section targeting: no candidate sections for {file_path.name}"
            )
            return None

        relevant_indices = self._confirm_with_llm(chunks, candidates)
        if not relevant_indices:
            return None

        filtered_text = "\n\n".join(
            chunks[i].to_text() for i in sorted(set(relevant_indices))
        )

        self._write_cache(file_path, filtered_text)
        return filtered_text
