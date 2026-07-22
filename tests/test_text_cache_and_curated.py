"""Tests for the shared text/OCR cache and curated-set materialization.

These cover the reuse + organization work: extracted text is cached next to a
document and reused (so OCR runs once), and the acquisition engine materializes
only the selected documents into a ``curated/`` tree, carrying the text cache
along so downstream extraction never re-OCRs.
"""

from __future__ import annotations

from pathlib import Path

from psweep.acquisition.engine import AcquisitionEngine
from psweep.extraction.document_utils import (
    extract_text_from_document,
    read_text_cache,
    write_text_cache,
)


class TestTextCache:
    def test_native_documents_are_not_cached(self, tmp_path: Path):
        # Cheap native extraction must NOT be cached — re-extracting keeps full
        # fidelity (tables etc.) and lets a later stage pick a richer method.
        doc = tmp_path / "doc.txt"
        doc.write_text("hello world " * 10, encoding="utf-8")
        extract_text_from_document(doc)
        assert not (tmp_path / ".text").exists()

    def test_ocr_result_is_cached_and_reused(self, tmp_path: Path, monkeypatch):
        # Simulate a scanned PDF whose extraction goes through the OCR path.
        pdf = tmp_path / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        calls = {"n": 0}

        def _fake(pdf_path, page_range=None, return_meta=False):
            calls["n"] += 1
            text = "OCR EXTRACTED TEXT"
            return (text, {"used_ocr": True}) if return_meta else text

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_text_from_pdf",
            _fake,
        )
        text1 = extract_text_from_document(pdf)
        assert (tmp_path / ".text" / "scan.txt").exists()
        assert calls["n"] == 1
        # Second call is served from cache — the extractor does not run again.
        text2 = extract_text_from_document(pdf)
        assert text2 == text1 and calls["n"] == 1

    def test_native_pdf_is_not_cached(self, tmp_path: Path, monkeypatch):
        pdf = tmp_path / "native.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        def _fake(pdf_path, page_range=None, return_meta=False):
            text = "NATIVE TEXT WITH TABLES"
            return (text, {"used_ocr": False}) if return_meta else text

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_text_from_pdf",
            _fake,
        )
        extract_text_from_document(pdf)
        assert not (tmp_path / ".text").exists()

    def test_cache_invalidated_when_source_changes(self, tmp_path: Path):
        doc = tmp_path / "doc.pdf"
        doc.write_text("original", encoding="utf-8")
        write_text_cache(doc, "cached-original")
        assert read_text_cache(doc) == "cached-original"

        # Editing the source (size/mtime change) invalidates the cache.
        doc.write_text("something much longer than before", encoding="utf-8")
        assert read_text_cache(doc) is None

    def test_page_range_bypasses_cache(self, tmp_path: Path, monkeypatch):
        doc = tmp_path / "doc.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        write_text_cache(doc, "FULL-CACHED")

        def _fake(pdf_path, page_range=None, return_meta=False):
            text = f"PAGE-RANGE {page_range}"
            return (text, {"used_ocr": False}) if return_meta else text

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_text_from_pdf",
            _fake,
        )
        # A page-range request must re-extract, not return the full cache.
        result = extract_text_from_document(doc, page_range=(1, 2))
        assert result != "FULL-CACHED"
        assert "PAGE-RANGE" in result


class TestMaterializeCurated:
    def _record(self, docs_dir: Path, rel: str, selected: bool) -> dict:
        path = docs_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pdf-bytes", encoding="utf-8")
        return {
            "path": path.as_posix(),
            "relative_path": rel,
            "review_selected": selected,
        }

    def test_only_selected_are_curated(self, tmp_path: Path):
        docs = tmp_path / "documents"
        keep = self._record(docs, "co/keep.pdf", True)
        drop = self._record(docs, "co/drop.pdf", False)

        curated_dir, count = AcquisitionEngine._materialize_curated(
            documents_dir=docs, download_records=[keep, drop]
        )
        assert count == 1
        assert (curated_dir / "co/keep.pdf").exists()
        assert not (curated_dir / "co/drop.pdf").exists()

    def test_no_review_curates_all_downloads(self, tmp_path: Path):
        # Domains without document_review have no review_selected on records;
        # every successfully-downloaded file should still be curated.
        docs = tmp_path / "documents"
        a = {
            "path": (docs / "h/a.pdf").as_posix(),
            "relative_path": "h/a.pdf",
            "status": "downloaded",
        }
        (docs / "h").mkdir(parents=True)
        (docs / "h/a.pdf").write_text("x", encoding="utf-8")
        b = {"status": "failed"}  # no path — must be skipped

        curated_dir, count = AcquisitionEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert count == 1
        assert (curated_dir / "h/a.pdf").exists()

    def test_text_cache_is_carried_into_curated(self, tmp_path: Path):
        docs = tmp_path / "documents"
        rec = self._record(docs, "co/scan.pdf", True)
        # Simulate an OCR cache produced during review.
        write_text_cache(Path(rec["path"]), "OCR TEXT")

        curated_dir, _ = AcquisitionEngine._materialize_curated(
            documents_dir=docs, download_records=[rec]
        )
        # The curated copy carries its cache, so extraction reuses it.
        assert read_text_cache(curated_dir / "co/scan.pdf") == "OCR TEXT"

    def test_rebuild_drops_stale_files(self, tmp_path: Path):
        docs = tmp_path / "documents"
        a = self._record(docs, "co/a.pdf", True)
        b = self._record(docs, "co/b.pdf", True)

        curated_dir, _ = AcquisitionEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert (curated_dir / "co/b.pdf").exists()

        # Re-materialize with b no longer selected → it must disappear.
        b["review_selected"] = False
        curated_dir, count = AcquisitionEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert count == 1
        assert (curated_dir / "co/a.pdf").exists()
        assert not (curated_dir / "co/b.pdf").exists()
