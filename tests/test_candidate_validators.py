"""Tests for ContentSampler: text extraction and keyword validation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from psweep.discovery.validators import ContentSampler

FIXTURE_DIR = Path(__file__).parent / "fixtures/content_samples"


def _fixture_path(name: str) -> Path:
    fixture_path = FIXTURE_DIR / name
    assert fixture_path.exists(), f"Required test fixture missing: {fixture_path}"
    return fixture_path


class TestContentSamplerTextExtraction:
    """Test text extraction from different file formats."""

    def test_extract_text_from_txt_file(self):
        """Test extraction from plain text file."""
        text = ContentSampler.extract_text(str(_fixture_path("geothermal_ordinance.txt")))
        assert "geothermal" in text.lower()
        assert "ordinance" in text.lower()
        assert len(text) > 100

    def test_extract_text_from_generic_txt(self):
        """Test extraction from generic text file."""
        text = ContentSampler.extract_text(str(_fixture_path("generic_document.txt")))
        assert "information" in text.lower()
        assert len(text) > 50

    def test_extract_text_unsupported_format_raises_error(self):
        """Test that unsupported formats raise ValueError."""
        with pytest.raises(ValueError):
            ContentSampler.extract_text("/path/to/file.zip")

    def test_extract_text_nonexistent_file_raises_error(self):
        """Test that nonexistent files raise error."""
        with pytest.raises(Exception):
            ContentSampler.extract_text("/nonexistent/path/file.txt")


class TestContentSamplerValidation:
    """Test content validation with keyword checking."""

    def test_validate_content_with_required_keywords_present(self):
        """Test validation passes when required keywords are found."""
        result = ContentSampler.validate_content(
            str(_fixture_path("geothermal_ordinance.txt")),
            required_keywords=["geothermal", "ordinance"],
            min_required_matches=2,
        )
        assert result.success is True
        assert len(result.keywords_found) == 2
        assert len(result.keywords_missing) == 0
        assert result.confidence_score >= 1.0

    def test_validate_content_with_required_keywords_missing(self):
        """Test validation fails when required keywords are missing."""
        result = ContentSampler.validate_content(
            str(_fixture_path("generic_document.txt")),
            required_keywords=["geothermal", "ordinance"],
            min_required_matches=1,
        )
        assert result.success is False
        assert len(result.keywords_found) == 0
        assert len(result.keywords_missing) == 2

    def test_validate_content_with_partial_keyword_matches(self):
        """Test validation with some keywords present, some missing."""
        result = ContentSampler.validate_content(
            str(_fixture_path("geothermal_ordinance.txt")),
            required_keywords=["geothermal", "tariff", "ordinance"],
            min_required_matches=1,
        )
        assert result.success is True
        assert len(result.keywords_found) == 2  # geothermal and ordinance
        assert len(result.keywords_missing) == 1  # tariff
        assert result.confidence_score >= 0.6

    def test_validate_content_case_insensitive(self):
        """Test that keyword matching is case-insensitive."""
        result = ContentSampler.validate_content(
            str(_fixture_path("geothermal_ordinance.txt")),
            required_keywords=["GEOTHERMAL", "ORDINANCE"],
            min_required_matches=2,
        )
        assert result.success is True
        assert len(result.keywords_found) == 2

    def test_validate_content_with_nice_to_have_keywords(self):
        """Test confidence score includes nice-to-have keywords."""
        result = ContentSampler.validate_content(
            str(_fixture_path("electricity_tariff.txt")),
            required_keywords=["tariff"],
            nice_to_have_keywords=["rate", "electricity", "energy"],
            min_required_matches=1,
        )
        assert result.success is True
        # Confidence should be higher due to nice-to-have matches
        assert result.confidence_score >= 0.7  # Some nice-to-have matched

    def test_validate_content_no_keywords_specified(self):
        """Test validation passes when no keywords are specified."""
        result = ContentSampler.validate_content(str(_fixture_path("generic_document.txt")))
        assert result.success is True
        assert result.confidence_score == 1.0

    def test_validate_content_insufficient_extraction_length(self):
        """Test validation fails when insufficient text is extracted."""
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
        fixture_path = _fixture_path("geothermal_ordinance.txt")
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
        fixture_path = _fixture_path("electricity_tariff.txt")
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
        # Test geothermal ordinance
        geothermal_path = _fixture_path("geothermal_ordinance.txt")
        tariff_path = _fixture_path("electricity_tariff.txt")
        solar_path = _fixture_path("solar_ordinance.txt")

        result = ContentSampler.validate_content(
            str(geothermal_path),
            required_keywords=["geothermal"],
            min_required_matches=1,
        )
        assert result.success is True

        result = ContentSampler.validate_content(
            str(tariff_path),
            required_keywords=["tariff", "rate"],
            min_required_matches=2,
        )
        assert result.success is True

        result = ContentSampler.validate_content(
            str(solar_path),
            required_keywords=["solar"],
            min_required_matches=1,
        )
        assert result.success is True

    def test_content_sampler_provides_detailed_reasons(self):
        """Test that ContentSampler provides detailed validation reasons."""
        result = ContentSampler.validate_content(
            str(_fixture_path("geothermal_ordinance.txt")),
            required_keywords=["geothermal"],
        )
        assert len(result.reasons) > 0
        assert any("characters" in r.lower() for r in result.reasons)
        assert any("keyword" in r.lower() or "found" in r.lower() for r in result.reasons)
