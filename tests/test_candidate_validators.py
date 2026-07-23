"""Tests for ContentSampler: text extraction and keyword validation."""

from __future__ import annotations

import pytest

from psweep.discovery.validators import ContentSampler


class TestContentSamplerTextExtraction:
    """Test text extraction from different file formats."""

    def test_extract_text_from_txt_file(self):
        """Test extraction from plain text file."""
        from psweep.discovery.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/geothermal_ordinance.txt"
        if fixture_path.exists():
            text = ContentSampler.extract_text(str(fixture_path))
            assert "geothermal" in text.lower()
            assert "ordinance" in text.lower()
            assert len(text) > 100

    def test_extract_text_from_generic_txt(self):
        """Test extraction from generic text file."""
        from psweep.discovery.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/generic_document.txt"
        if fixture_path.exists():
            text = ContentSampler.extract_text(str(fixture_path))
            assert "information" in text.lower()
            assert len(text) > 50

    def test_extract_text_unsupported_format_raises_error(self):
        """Test that unsupported formats raise ValueError."""
        from psweep.discovery.validators import ContentSampler

        with pytest.raises(ValueError):
            ContentSampler.extract_text("/path/to/file.zip")

    def test_extract_text_nonexistent_file_raises_error(self):
        """Test that nonexistent files raise error."""
        from psweep.discovery.validators import ContentSampler

        with pytest.raises(Exception):
            ContentSampler.extract_text("/nonexistent/path/file.txt")


class TestContentSamplerValidation:
    """Test content validation with keyword checking."""

    def test_validate_content_with_required_keywords_present(self):
        """Test validation passes when required keywords are found."""
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures/content_samples/generic_document.txt"
        if fixture_path.exists():
            result = ContentSampler.validate_content(str(fixture_path))
            assert result.success is True
            assert result.confidence_score == 1.0

    def test_validate_content_insufficient_extraction_length(self):
        """Test validation fails when insufficient text is extracted."""
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
        from psweep.discovery.validators import ContentSampler
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
