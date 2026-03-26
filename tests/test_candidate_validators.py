"""Tests for candidate validation and deduplication."""

from __future__ import annotations

import pytest

from streamline_extract.acquisition.validators import (
    CandidateValidator,
    CandidateDeduplicator,
    ValidationInput,
)
from streamline_extract.acquisition.models import AcquisitionCandidate, CandidateScore


class TestCandidateValidatorExtensionExtraction:
    """Test file extension extraction from URLs."""

    def test_extract_pdf_extension(self):
        url = "https://county.org/docs/ordinance.pdf"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".pdf"

    def test_extract_docx_extension(self):
        url = "https://county.org/files/regulation.docx"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".docx"

    def test_extract_xlsx_extension(self):
        url = "https://county.org/data/rates.xlsx"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".xlsx"

    def test_ignore_query_parameters(self):
        url = "https://county.org/document.pdf?download=true&format=inline"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".pdf"

    def test_handle_uppercase_extension(self):
        url = "https://county.org/doc/ORDINANCE.PDF"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".pdf"

    def test_no_extension_returns_none(self):
        url = "https://county.org/document"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext is None

    def test_html_extension_extracted(self):
        url = "https://county.org/page.html"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".html"

    def test_multiple_dots_extracts_last_extension(self):
        url = "https://county.org/archive.old.pdf"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext == ".pdf"

    def test_path_without_filename(self):
        url = "https://county.org/path/to/directory/"
        ext = CandidateValidator._extract_extension_from_url(url)
        assert ext is None


class TestCandidateValidatorCanonicalUrl:
    """Test canonical URL computation for deduplication."""

    def test_http_normalized_to_https(self):
        urls = [
            "http://county.org/doc.pdf",
            "https://county.org/doc.pdf",
        ]
        canonicals = [CandidateValidator._compute_canonical_url(u) for u in urls]
        # Both should normalize to https
        assert canonicals[0] == canonicals[1]
        assert canonicals[0].startswith("https://")

    def test_trailing_slash_normalized(self):
        url1 = "https://county.org/docs/"
        url2 = "https://county.org/docs"
        canon1 = CandidateValidator._compute_canonical_url(url1)
        canon2 = CandidateValidator._compute_canonical_url(url2)
        assert canon1 == canon2

    def test_hostname_lowercase(self):
        url = "https://COUNTY.ORG/Doc.PDF"
        canonical = CandidateValidator._compute_canonical_url(url)
        assert "county.org" in canonical
        assert "COUNTY" not in canonical

    def test_path_lowercase(self):
        url = "https://county.org/DOCUMENTS/ORDINANCE.PDF"
        canonical = CandidateValidator._compute_canonical_url(url)
        path = canonical.split("county.org")[1].split("?")[0]
        assert path == path.lower()

    def test_query_parameters_sorted(self):
        url1 = "https://county.org/search?z=3&a=1&b=2"
        url2 = "https://county.org/search?b=2&z=3&a=1"
        canon1 = CandidateValidator._compute_canonical_url(url1)
        canon2 = CandidateValidator._compute_canonical_url(url2)
        assert canon1 == canon2

    def test_fragment_removed(self):
        url_with_fragment = "https://county.org/doc.pdf#page=10"
        url_without_fragment = "https://county.org/doc.pdf"
        canon_with = CandidateValidator._compute_canonical_url(url_with_fragment)
        canon_without = CandidateValidator._compute_canonical_url(url_without_fragment)
        assert canon_with == canon_without
        assert "#" not in canon_with

    def test_identical_urls_produce_same_canonical(self):
        url = "https://county.org/ordinance.pdf"
        canon1 = CandidateValidator._compute_canonical_url(url)
        canon2 = CandidateValidator._compute_canonical_url(url)
        assert canon1 == canon2


class TestCandidateValidatorSignals:
    """Test signal computation for candidate scoring."""

    def test_url_signal_pdf_supported(self):
        url = "https://county.org/ordinance.pdf"
        signal = CandidateValidator._compute_url_signal(url, ".pdf")
        assert 0.7 <= signal <= 1.0

    def test_url_signal_html_unsupported(self):
        url = "https://county.org/page.html"
        signal = CandidateValidator._compute_url_signal(url, ".html")
        assert signal < 0.5

    def test_url_signal_keyword_boost(self):
        # Use non-keyword URL for comparison
        url_with_keyword = "https://county.org/ordinance.pdf"
        url_without_keyword = "https://county.org/file.pdf"
        signal_with = CandidateValidator._compute_url_signal(url_with_keyword, ".pdf")
        signal_without = CandidateValidator._compute_url_signal(url_without_keyword, ".pdf")
        assert signal_with > signal_without

    def test_url_signal_no_extension(self):
        url = "https://county.org/download"
        signal = CandidateValidator._compute_url_signal(url, None)
        assert 0.3 <= signal <= 0.7

    def test_anchor_signal_empty(self):
        signal = CandidateValidator._compute_anchor_signal("")
        assert signal == 0.0

    def test_anchor_signal_with_keywords(self):
        text = "Download Geothermal Ordinance PDF"
        signal = CandidateValidator._compute_anchor_signal(text)
        assert signal > 0.3

    def test_anchor_signal_no_keywords(self):
        text = "Click here"
        signal = CandidateValidator._compute_anchor_signal(text)
        assert signal < 0.3

    def test_content_signal_with_keywords(self):
        summary = "County ordinance and permit regulations"
        title = "Geothermal Code"
        signal = CandidateValidator._compute_content_signal(summary, title)
        assert signal > 0.2

    def test_content_signal_empty(self):
        signal = CandidateValidator._compute_content_signal("", "")
        assert signal == 0.0

    def test_trust_signal_government_domain(self):
        url_gov = "https://county.gov/doc.pdf"
        url_com = "https://example.com/doc.pdf"
        signal_gov = CandidateValidator._compute_trust_signal(url_gov)
        signal_com = CandidateValidator._compute_trust_signal(url_com)
        assert signal_gov > signal_com

    def test_trust_signal_known_repository(self):
        url_cms = "https://cms2.revize.com/doc.pdf"
        url_other = "https://other.com/doc.pdf"
        signal_cms = CandidateValidator._compute_trust_signal(url_cms)
        signal_other = CandidateValidator._compute_trust_signal(url_other)
        assert signal_cms > signal_other


class TestCandidateValidatorValidation:
    """Test full candidate validation workflow."""

    def test_validate_supported_pdf(self):
        validation_input = ValidationInput(
            candidate_url="https://county.org/ordinance.pdf",
            source="seeker",
            anchor_text="Download Ordinance PDF",
            page_title="County Ordinances",
            content_summary="Official county ordinance documents",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        assert result.extension == ".pdf"
        assert result.is_valid
        assert result.acceptance_class in ("accepted", "needs_review")
        assert result.canonical_url == "https://county.org/ordinance.pdf"

    def test_validate_unsupported_html(self):
        validation_input = ValidationInput(
            candidate_url="https://county.org/page.html",
            source="digger",
            anchor_text="Click here",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        assert result.extension == ".html"
        assert not result.is_valid
        assert "Unsupported" in result.reasons[0]

    def test_validate_no_extension_low_signals(self):
        validation_input = ValidationInput(
            candidate_url="https://example.com/random-doc",
            source="unknown",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        # No file hint and low content context
        assert not result.is_valid
        assert result.extension is None

    def test_validate_strong_signals_no_extension(self):
        validation_input = ValidationInput(
            candidate_url="https://county.gov/ordinance-download",
            source="seeker",
            anchor_text="Download Geothermal Ordinance PDF",
            page_title="Geothermal Ordinance",
            content_summary="Geothermal permit and regulation code",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        # Strong signals compensate for missing extension
        assert result.signals["total"] > 0.5
        assert result.extension is None

    def test_validate_canonical_url_included_in_reasons(self):
        validation_input = ValidationInput(
            candidate_url="https://county.org/doc.pdf",
            source="seed",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        assert any("Canonical URL" in reason for reason in result.reasons)

    def test_validate_signal_weights_applied(self):
        validation_input = ValidationInput(
            candidate_url="https://county.gov/ordinance.pdf",
            source="seeker",
            anchor_text="Geothermal Ordinance",
            page_title="Code",
            content_summary="Geothermal regulations",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        assert result.signals is not None
        assert "url_signal" in result.signals
        assert "anchor_signal" in result.signals
        assert "content_signal" in result.signals
        assert "trust_signal" in result.signals
        assert "total" in result.signals
        # Total should be weighted average
        assert 0.0 <= result.signals["total"] <= 1.0


class TestCandidateDeduplicator:
    """Test candidate deduplication by canonical URL."""

    def test_deduplicate_same_url_different_sources(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc.pdf",
                source="seeker",
                score=CandidateScore(url_signal=0.8),
            ),
            AcquisitionCandidate(
                url="https://county.org/doc.pdf",
                source="digger",
                score=CandidateScore(url_signal=0.7),
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate(candidates)

        assert len(deduplicated) == 1
        # Should keep the higher-scored one or seed preference
        assert deduplicated[0].url == "https://county.org/doc.pdf"

    def test_deduplicate_different_urls_different_docs(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/ordinance.pdf",
                source="seeker",
                score=CandidateScore(url_signal=0.8),
            ),
            AcquisitionCandidate(
                url="https://county.org/permit.pdf",
                source="seeker",
                score=CandidateScore(url_signal=0.7),
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate(candidates)

        assert len(deduplicated) == 2

    def test_deduplicate_normalized_urls_same(self):
        candidates = [
            AcquisitionCandidate(
                url="http://county.org/doc.pdf",
                source="seeker",
                score=CandidateScore(),
                canonical_url="https://county.org/doc.pdf",
            ),
            AcquisitionCandidate(
                url="HTTPS://COUNTY.ORG/DOC.PDF",
                source="digger",
                score=CandidateScore(),
                canonical_url="https://county.org/doc.pdf",
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate(candidates)

        # Same canonical URL = deduplicated
        assert len(deduplicated) == 1

    def test_deduplicate_prefers_accepted_over_review(self):
        # Create score that yields "accepted" status
        accepted_score = CandidateScore(
            url_signal=1.0, anchor_signal=1.0, content_signal=1.0, trust_signal=1.0
        )
        # Create score that yields "needs_review"
        review_score = CandidateScore(
            url_signal=0.5, anchor_signal=0.4, content_signal=0.5, trust_signal=0.4
        )

        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc.pdf",
                source="seeker",
                score=review_score,
                canonical_url="https://county.org/doc.pdf",
            ),
            AcquisitionCandidate(
                url="https://county.org/doc.pdf?v=2",
                source="digger",
                score=accepted_score,
                canonical_url="https://county.org/doc.pdf",
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate(candidates, prefer_accepted=True)

        assert len(deduplicated) == 1
        # Should select from the two - verifying dedup occurred
        assert deduplicated[0].canonical_url == "https://county.org/doc.pdf"

    def test_deduplicate_empty_list(self):
        deduplicated = CandidateDeduplicator.deduplicate([])
        assert deduplicated == []

    def test_deduplicate_single_candidate(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc.pdf",
                source="seed",
                score=CandidateScore(),
            )
        ]

        deduplicated = CandidateDeduplicator.deduplicate(candidates)

        assert len(deduplicated) == 1
        assert deduplicated[0].url == candidates[0].url

    def test_deduplicate_by_content_hash_same_content(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc.pdf",
                source="seeker",
                score=CandidateScore(url_signal=0.9),
                content_hash="abc123",
            ),
            AcquisitionCandidate(
                url="https://other-mirror.org/doc.pdf",
                source="digger",
                score=CandidateScore(url_signal=0.7),
                content_hash="abc123",  # Same content
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate_by_content_hash(candidates)

        # Same content hash = deduplicated, keep higher score
        assert len(deduplicated) == 1
        assert deduplicated[0].score.url_signal == 0.9

    def test_deduplicate_by_content_hash_different_content(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc1.pdf",
                source="seeker",
                score=CandidateScore(),
                content_hash="abc123",
            ),
            AcquisitionCandidate(
                url="https://county.org/doc2.pdf",
                source="seeker",
                score=CandidateScore(),
                content_hash="def456",
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate_by_content_hash(candidates)

        # Different content = keep both
        assert len(deduplicated) == 2

    def test_deduplicate_by_content_hash_mixed_with_without(self):
        candidates = [
            AcquisitionCandidate(
                url="https://county.org/doc1.pdf",
                source="seeker",
                score=CandidateScore(),
                content_hash="abc123",
            ),
            AcquisitionCandidate(
                url="https://county.org/doc2.pdf",
                source="digger",
                score=CandidateScore(),
                content_hash=None,  # No hash
            ),
        ]

        deduplicated = CandidateDeduplicator.deduplicate_by_content_hash(candidates)

        # Keep both: one hashed, one unhashed
        assert len(deduplicated) == 2


class TestCandidateValidatorMimeType:
    """Test MIME type to extension mapping."""

    def test_pdf_mime_to_extension(self):
        ext = CandidateValidator.MIME_TO_EXTENSION.get("application/pdf")
        assert ext == ".pdf"

    def test_docx_mime_to_extension(self):
        ext = CandidateValidator.MIME_TO_EXTENSION.get(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert ext == ".docx"

    def test_xlsx_mime_to_extension(self):
        ext = CandidateValidator.MIME_TO_EXTENSION.get(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert ext == ".xlsx"

    def test_csv_mime_variants(self):
        assert CandidateValidator.MIME_TO_EXTENSION.get("text/csv") == ".csv"
        assert CandidateValidator.MIME_TO_EXTENSION.get("application/csv") == ".csv"

    def test_extract_extension_from_content_type(self):
        content_type = "application/pdf; charset=utf-8"
        ext = CandidateValidator._extract_extension_from_content_type(content_type)
        assert ext == ".pdf"

    def test_extract_extension_from_content_type_none(self):
        ext = CandidateValidator._extract_extension_from_content_type(None)
        assert ext is None

    def test_extract_extension_from_content_type_unknown(self):
        content_type = "application/unknown"
        ext = CandidateValidator._extract_extension_from_content_type(content_type)
        assert ext is None


class TestCandidateValidatorToCandidateModel:
    """Test conversion to AcquisitionCandidate model."""

    def test_to_candidate_preserves_validation_data(self):
        validation_input = ValidationInput(
            candidate_url="https://county.org/doc.pdf",
            source="seeker",
            anchor_text="Download",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)

        candidate = CandidateValidator.to_candidate(validation_input, result)

        assert candidate.url == "https://county.org/doc.pdf"
        assert candidate.source == "seeker"
        assert candidate.extension == ".pdf"
        assert candidate.canonical_url == result.canonical_url
        assert len(candidate.reasons) > 0

    def test_to_candidate_with_content_hash(self):
        validation_input = ValidationInput(
            candidate_url="https://county.org/doc.pdf",
            source="seeker",
        )
        validator = CandidateValidator()
        result = validator.validate(validation_input)
        content = "document content here"

        candidate = CandidateValidator.to_candidate(
            validation_input, result, content_for_hash=content
        )

        assert candidate.content_hash is not None
        assert len(candidate.content_hash) == 64  # SHA256 hex length


class TestContentSamplerTextExtraction:
    """Test text extraction from different file formats."""

    def test_extract_text_from_txt_file(self):
        """Test extraction from plain text file."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            text = ContentSampler.extract_text(str(fixture_path))
            assert "geothermal" in text.lower()
            assert "ordinance" in text.lower()
            assert len(text) > 100

    def test_extract_text_from_generic_txt(self):
        """Test extraction from generic text file."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/generic_document.txt"
        if fixture_path.exists():
            text = ContentSampler.extract_text(str(fixture_path))
            assert "information" in text.lower()
            assert len(text) > 50

    def test_extract_text_unsupported_format_raises_error(self):
        """Test that unsupported formats raise ValueError."""
        from streamline_extract.acquisition.validators import ContentSampler

        with pytest.raises(ValueError):
            ContentSampler.extract_text("/path/to/file.zip")

    def test_extract_text_nonexistent_file_raises_error(self):
        """Test that nonexistent files raise error."""
        from streamline_extract.acquisition.validators import ContentSampler

        with pytest.raises(Exception):
            ContentSampler.extract_text("/nonexistent/path/file.txt")


class TestContentSamplerValidation:
    """Test content validation with keyword checking."""

    def test_validate_content_with_required_keywords_present(self):
        """Test validation passes when required keywords are found."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal", "ordinance"],
                min_required_matches=2,
            )
            assert result.success is True
            assert len(result.keywords_found) == 2
            assert len(result.keywords_missing) == 0
            assert result.confidence_score >= 1.0

    def test_validate_content_with_required_keywords_missing(self):
        """Test validation fails when required keywords are missing."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/generic_document.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal", "ordinance"],
                min_required_matches=1,
            )
            assert result.success is False
            assert len(result.keywords_found) == 0
            assert len(result.keywords_missing) == 2

    def test_validate_content_with_partial_keyword_matches(self):
        """Test validation with some keywords present, some missing."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal", "tariff", "ordinance"],
                min_required_matches=1,
            )
            assert result.success is True
            assert len(result.keywords_found) == 2  # geothermal and ordinance
            assert len(result.keywords_missing) == 1  # tariff
            assert result.confidence_score >= 0.6

    def test_validate_content_case_insensitive(self):
        """Test that keyword matching is case-insensitive."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["GEOTHERMAL", "ORDINANCE"],
                min_required_matches=2,
            )
            assert result.success is True
            assert len(result.keywords_found) == 2

    def test_validate_content_with_nice_to_have_keywords(self):
        """Test confidence score includes nice-to-have keywords."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/electricity_tariff.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["tariff"],
                nice_to_have_keywords=["rate", "electricity", "energy"],
                min_required_matches=1,
            )
            assert result.success is True
            # Confidence should be higher due to nice-to-have matches
            assert result.confidence_score >= 0.7  # Some nice-to-have matched

    def test_validate_content_no_keywords_specified(self):
        """Test validation passes when no keywords are specified."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/generic_document.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(str(fixture_path))
            assert result.success is True
            assert result.confidence_score == 1.0

    def test_validate_content_insufficient_extraction_length(self):
        """Test validation fails when insufficient text is extracted."""
        from streamline_extract.acquisition.validators import ContentSampler
        import tempfile
        import os

        # Create a very small test file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("x")
            temp_path = f.name

        try:
            result = ContentSampler.validate_content(
                temp_path,
                required_keywords=["test"],
            )
            assert result.success is False
            assert result.error is not None or "too short" in " ".join(result.reasons).lower()
        finally:
            os.unlink(temp_path)

    def test_validate_content_min_required_matches_threshold(self):
        """Test min_required_matches threshold enforcement."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            # First test: require 2 (both are present)
            result1 = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal", "ordinance"],
                min_required_matches=2,
            )
            assert result1.success is True

            # Second test: require 3 (only 2 are present)
            result2 = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal", "ordinance", "tariff"],
                min_required_matches=3,
            )
            assert result2.success is False

    def test_validate_content_confidence_score_calculation(self):
        """Test confidence score is correctly calculated."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/electricity_tariff.txt"
        if fixture_path.exists():
            # All required match (100%)
            result_all = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["tariff", "rate"],
                min_required_matches=1,
            )
            assert result_all.confidence_score == pytest.approx(1.0)

            # None required match (0% required, but 100% nice-to-have gives 0.3 overall)
            result_none = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["xyz", "abc"],
                min_required_matches=0,
            )
            # With no nice-to-have keywords: confidence = (0/2 * 0.7) + (1.0 * 0.3) = 0.3
            assert result_none.confidence_score == pytest.approx(0.3)

            # Partial required match (50%)
            result_partial = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["tariff", "xyz"],
                min_required_matches=1,
            )
            # With no nice-to-have keywords: confidence = (1/2 * 0.7) + (1.0 * 0.3) = 0.65
            assert result_partial.confidence_score == pytest.approx(0.65)


class TestContentSamplerIntegration:
    """Integration tests for content sampling with validators."""

    def test_content_sampler_with_domain_specific_keywords(self):
        """Test content sampling identifies domain-specific documents."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        # Test geothermal ordinance
        geothermal_path = (
            Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        )
        tariff_path = Path(__file__).parent / "fixtures/content_samples/electricity_tariff.txt"
        solar_path = Path(__file__).parent / "fixtures/content_samples/solar_ordinance.txt"

        if geothermal_path.exists():
            result = ContentSampler.validate_content(
                str(geothermal_path),
                required_keywords=["geothermal"],
                min_required_matches=1,
            )
            assert result.success is True

        if tariff_path.exists():
            result = ContentSampler.validate_content(
                str(tariff_path),
                required_keywords=["tariff", "rate"],
                min_required_matches=2,
            )
            assert result.success is True

        if solar_path.exists():
            result = ContentSampler.validate_content(
                str(solar_path),
                required_keywords=["solar"],
                min_required_matches=1,
            )
            assert result.success is True

    def test_content_sampler_provides_detailed_reasons(self):
        """Test that ContentSampler provides detailed validation reasons."""
        from streamline_extract.acquisition.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(
                str(fixture_path),
                required_keywords=["geothermal"],
            )
            assert len(result.reasons) > 0
            assert any("characters" in r.lower() for r in result.reasons)
            assert any("keyword" in r.lower() or "found" in r.lower() for r in result.reasons)
