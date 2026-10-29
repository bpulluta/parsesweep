"""Tests for the shared text/OCR cache and curated-set materialization.

These cover the reuse + organization work: extracted text is cached next to a
document and reused (so OCR runs once), and the discovery engine materializes
only the selected documents into a ``curated/`` tree, carrying the text cache
along so downstream extraction never re-OCRs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from psweep.discovery.engine import DiscoveryEngine
from psweep.extraction.document_utils import (
    extract_text_from_document,
    read_text_cache,
    text_cache_paths,
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

        def _fake(pdf_path, page_range=None, return_meta=False, ocr_corrections=None):
            calls["n"] += 1
            text = "OCR EXTRACTED TEXT"
            return (text, {"used_ocr": True}) if return_meta else text

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_text_from_pdf",
            _fake,
        )
        text1 = extract_text_from_document(pdf)
        assert (tmp_path / ".text" / "scan.pdf.txt").exists()
        assert calls["n"] == 1
        # Second call is served from cache — the extractor does not run again.
        text2 = extract_text_from_document(pdf)
        assert text2 == text1 and calls["n"] == 1

    def test_native_pdf_is_not_cached(self, tmp_path: Path, monkeypatch):
        pdf = tmp_path / "native.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        def _fake(pdf_path, page_range=None, return_meta=False, ocr_corrections=None):
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

    def test_same_stem_formats_have_independent_cache_pairs(self, tmp_path):
        pdf, html = tmp_path / "report.pdf", tmp_path / "report.html"
        for source in (pdf, html):
            source.write_bytes(b"same bytes")
            os.utime(source, ns=(1000000000, 1000000000))
        write_text_cache(pdf, "PDF OCR")
        write_text_cache(html, "HTML TEXT")

        assert text_cache_paths(pdf) == (
            tmp_path / ".text/report.pdf.txt",
            tmp_path / ".text/report.pdf.meta.json",
        )
        assert read_text_cache(pdf) == "PDF OCR"
        assert read_text_cache(html) == "HTML TEXT"
        assert len(list((tmp_path / ".text").iterdir())) == 4

    def test_legacy_stem_cache_is_not_reused(self, tmp_path):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source")
        write_text_cache(source, "old cache")
        text_path, meta_path = text_cache_paths(source)
        text_path.replace(text_path.parent / "report.txt")
        meta_path.replace(meta_path.parent / "report.meta.json")
        assert read_text_cache(source) is None

    def test_source_digest_invalidates_same_size_and_mtime(self, tmp_path):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"before")
        stat = source.stat()
        write_text_cache(source, "cached before")
        source.write_bytes(b"after!")
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert read_text_cache(source) is None

    def test_text_digest_rejects_tampered_cache(self, tmp_path):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source")
        write_text_cache(source, "cached text")
        text_path, meta_path = text_cache_paths(source)
        meta = json.loads(meta_path.read_text())
        assert meta["source_sha256"] == hashlib.sha256(b"source").hexdigest()
        assert meta["text_sha256"] == hashlib.sha256(
            b"cached text",
        ).hexdigest()
        text_path.write_text("altered txt")  # same length, different digest
        assert read_text_cache(source) is None

    @pytest.mark.parametrize("missing", [0, 1])
    def test_partial_cache_pair_is_a_miss(self, tmp_path, missing):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source")
        write_text_cache(source, "cached text")
        text_cache_paths(source)[missing].unlink()
        assert read_text_cache(source) is None

    def test_metadata_replaced_last_and_intermediate_pair_is_a_miss(
        self, tmp_path, monkeypatch,
    ):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source")
        write_text_cache(source, "before")
        text_path, meta_path = text_cache_paths(source)
        original_replace = Path.replace
        destinations = []

        def observe_replace(staged, destination):
            assert staged.parent == destination.parent
            if destination == meta_path:
                assert text_path.read_text() == "after"
                assert read_text_cache(source) is None
            destinations.append(destination)
            return original_replace(staged, destination)

        monkeypatch.setattr(Path, "replace", observe_replace)
        write_text_cache(source, "after")
        assert destinations == [text_path, meta_path]
        assert read_text_cache(source) == "after"
        assert len(list(text_path.parent.iterdir())) == 2

    @pytest.mark.parametrize("failed_index", [0, 1])
    def test_interrupted_replace_never_serves_mismatched_pair(
        self, tmp_path, monkeypatch, failed_index,
    ):
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source")
        write_text_cache(source, "before")
        paths = text_cache_paths(source)
        original_replace = Path.replace

        def fail_replace(staged, destination):
            if destination == paths[failed_index]:
                raise OSError("interrupted cache write")
            return original_replace(staged, destination)

        monkeypatch.setattr(Path, "replace", fail_replace)
        write_text_cache(source, "after")  # caching must stay best-effort
        expected = "before" if failed_index == 0 else None
        assert read_text_cache(source) == expected
        assert len(list(paths[0].parent.iterdir())) == 2
        monkeypatch.setattr(Path, "replace", original_replace)
        write_text_cache(source, "recovered")
        assert read_text_cache(source) == "recovered"

    def test_page_range_bypasses_cache(self, tmp_path: Path, monkeypatch):
        doc = tmp_path / "doc.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        write_text_cache(doc, "FULL-CACHED")

        def _fake(pdf_path, page_range=None, return_meta=False, ocr_corrections=None):
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

        curated_dir, count, _ = DiscoveryEngine._materialize_curated(
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

        curated_dir, count, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert count == 1
        assert (curated_dir / "h/a.pdf").exists()

    def test_text_cache_is_carried_into_curated(self, tmp_path: Path):
        docs = tmp_path / "documents"
        rec = self._record(docs, "co/scan.pdf", True)
        # Simulate an OCR cache produced during review.
        write_text_cache(Path(rec["path"]), "OCR TEXT")

        curated_dir, _, _2 = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[rec]
        )
        # The curated copy carries its cache, so extraction reuses it.
        assert read_text_cache(curated_dir / "co/scan.pdf") == "OCR TEXT"

    def test_same_stem_cache_pairs_are_carried_without_collision(
        self, tmp_path,
    ):
        docs = tmp_path / "documents"
        pdf = self._record(docs, "co/report.pdf", True)
        html = self._record(docs, "co/report.html", True)
        write_text_cache(Path(pdf["path"]), "PDF TEXT")
        write_text_cache(Path(html["path"]), "HTML TEXT")

        curated, count, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[pdf, html],
        )
        assert count == 2
        assert read_text_cache(curated / "co/report.pdf") == "PDF TEXT"
        assert read_text_cache(curated / "co/report.html") == "HTML TEXT"

    def test_rebuild_drops_stale_files(self, tmp_path: Path):
        docs = tmp_path / "documents"
        a = self._record(docs, "co/a.pdf", True)
        b = self._record(docs, "co/b.pdf", True)

        curated_dir, _, _2 = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert (curated_dir / "co/b.pdf").exists()

        # Re-materialize with b no longer selected → it must disappear.
        b["review_selected"] = False
        curated_dir, count, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[a, b]
        )
        assert count == 1
        assert (curated_dir / "co/a.pdf").exists()
        assert not (curated_dir / "co/b.pdf").exists()


class TestDocumentRetention:
    def _record(self, docs_dir: Path, rel: str, selected: bool) -> dict:
        path = docs_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")
        return {
            "path": path.as_posix(),
            "relative_path": rel,
            "review_selected": selected,
            "status": "downloaded",
        }

    def test_retention_all_keeps_all_documents(self, tmp_path: Path):
        docs = tmp_path / "documents"
        keep = self._record(docs, "a/keep.pdf", True)
        drop = self._record(docs, "a/drop.pdf", False)
        curated, _, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[keep, drop]
        )

        removed_files, _ = DiscoveryEngine._apply_document_retention(
            mode="all",
            documents_dir=docs,
            curated_dir=curated,
            download_records=[keep, drop],
        )
        assert removed_files == 0
        assert (docs / "a/keep.pdf").exists()
        assert (docs / "a/drop.pdf").exists()

    def test_retention_curated_removes_non_selected(self, tmp_path: Path):
        docs = tmp_path / "documents"
        keep = self._record(docs, "a/keep.pdf", True)
        drop = self._record(docs, "a/drop.pdf", False)
        curated, _, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[keep, drop]
        )

        removed_files, _ = DiscoveryEngine._apply_document_retention(
            mode="curated",
            documents_dir=docs,
            curated_dir=curated,
            download_records=[keep, drop],
        )
        assert removed_files == 1
        assert (docs / "a/keep.pdf").exists()
        assert not (docs / "a/drop.pdf").exists()

    def test_retention_none_removes_all_documents(self, tmp_path: Path):
        docs = tmp_path / "documents"
        keep = self._record(docs, "a/keep.pdf", True)
        drop = self._record(docs, "a/drop.pdf", False)
        curated, _, _ = DiscoveryEngine._materialize_curated(
            documents_dir=docs, download_records=[keep, drop]
        )

        removed_files, _ = DiscoveryEngine._apply_document_retention(
            mode="none",
            documents_dir=docs,
            curated_dir=curated,
            download_records=[keep, drop],
        )
        assert removed_files == 3
        assert not (docs / "a/keep.pdf").exists()
        assert not (docs / "a/drop.pdf").exists()
        assert not (curated / "a/keep.pdf").exists()
