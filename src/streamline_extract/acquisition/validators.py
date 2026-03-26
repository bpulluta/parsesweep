"""Candidate validation and scoring for acquisition.

Validators handle:
- MIME type and file extension matching
- Canonical URL normalization and deduplication
- Keyword/content sampling validation
- Acceptance classification with signal weighting
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode

from .models import AcquisitionCandidate, CandidateScore


@dataclass(slots=True)
class ValidationInput:
    """Input for candidate validation."""

    candidate_url: str
    source: str = "unknown"
    anchor_text: str = ""
    page_title: str = ""
    content_summary: str = ""
    extra_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class ValidationResult:
    """Result of candidate validation."""

    is_valid: bool
    acceptance_class: str
    reasons: list[str]
    mime_type: str | None = None
    extension: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    signals: dict[str, float] | None = None


class CandidateValidator:
    """Validates and scores acquisition candidates."""

    # Supported file types for extraction
    SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".xlsx", ".csv"}

    # MIME type to extension mapping
    MIME_TO_EXTENSION = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.macroEnabled.document": ".docm",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "application/csv": ".csv",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/x-xlsx": ".xlsx",
    }

    # Common unsupported extensions
    UNSUPPORTED_EXTENSIONS = {
        ".html",
        ".htm",
        ".js",
        ".css",
        ".json",
        ".xml",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
    }

    @staticmethod
    def _extract_extension_from_url(url: str) -> str | None:
        """Extract file extension from URL path.

        Handles query parameters and common CDN patterns.
        """
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()

            # Remove common CDN/tracker parameters
            if "?" in path:
                path = path.split("?")[0]

            # Extract extension from path
            if "." in path:
                parts = path.rsplit(".", 1)
                if len(parts) == 2:
                    ext = "." + parts[1]
                    # Limit extension length (common: pdf=3, docx=4)
                    if len(ext) <= 6:
                        return ext
            return None
        except Exception:
            return None

    @staticmethod
    def _compute_canonical_url(url: str) -> str:
        """Compute canonical form of URL for deduplication.

        Normalizes:
        - Protocol (http -> https)
        - Path trailing slashes
        - Query parameter order
        - Fragment (removed)
        """
        try:
            parsed = urlparse(url)

            # Normalize protocol
            scheme = "https" if parsed.scheme.lower() in ("http", "https") else parsed.scheme.lower()

            # Normalize hostname
            netloc = parsed.netloc.lower().rstrip(":")

            # Normalize path
            path = parsed.path.lower()
            if path.endswith("/") and len(path) > 1:
                path = path.rstrip("/")

            # Normalize query parameters (sort for consistency)
            query = ""
            if parsed.query:
                params = parse_qs(parsed.query, keep_blank_values=True)
                # Sort parameters by key
                sorted_params = sorted(
                    (k, sorted(v) if isinstance(v, list) else v) for k, v in params.items()
                )
                query = urlencode(sorted_params, doseq=True)

            # Reconstruct without fragment
            canonical = f"{scheme}://{netloc}{path}"
            if query:
                canonical += f"?{query}"

            return canonical
        except Exception:
            return url.lower()

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        """Compute SHA256 hash of content for deduplication."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_extension_from_content_type(content_type: str | None) -> str | None:
        """Extract extension from Content-Type header."""
        if not content_type:
            return None

        # Extract MIME type (before semicolon)
        mime = content_type.split(";")[0].strip().lower()
        return CandidateValidator.MIME_TO_EXTENSION.get(mime)

    @staticmethod
    def _compute_url_signal(url: str, extension: str | None) -> float:
        """Compute URL-based signal (0.0 - 1.0).

        Rewards:
        - Supported file extensions (.pdf, .docx, etc.)
        - Keyword hints in URL (ordinance, permit, tariff, etc.)
        Penalizes:
        - Unsupported extensions (.html, .zip, etc.)
        - CDN/cache URLs with generic names
        """
        score = 0.5  # Base score

        if extension:
            if extension.lower() in CandidateValidator.SUPPORTED_EXTENSIONS:
                score += 0.3  # Strong reward for supported type
            elif extension.lower() in CandidateValidator.UNSUPPORTED_EXTENSIONS:
                score -= 0.2  # Penalty for unsupported type

        # Reward for domain keywords
        url_lower = url.lower()
        keyword_reward = 0.0
        if any(
            kw in url_lower
            for kw in [
                "ordinance",
                "permit",
                "regulation",
                "tariff",
                "rate",
                "schedule",
                "code",
                "document",
                "doc",
            ]
        ):
            keyword_reward = 0.1

        score = min(1.0, max(0.0, score + keyword_reward))
        return score

    @staticmethod
    def _compute_anchor_signal(anchor_text: str) -> float:
        """Compute anchor text signal (0.0 - 1.0).

        Rewards presence of domain-specific keywords in link text.
        """
        if not anchor_text or not anchor_text.strip():
            return 0.0

        text_lower = anchor_text.lower()
        keywords = [
            "ordinance",
            "permit",
            "regulation",
            "tariff",
            "rate",
            "schedule",
            "pdf",
            "download",
            "code",
        ]

        matching = sum(1 for kw in keywords if kw in text_lower)
        # Higher score for more keyword matches
        return min(1.0, matching * 0.2)

    @staticmethod
    def _compute_content_signal(content_summary: str, page_title: str) -> float:
        """Compute content-based signal (0.0 - 1.0).

        Rewards pages with domain keywords and clear document context.
        """
        combined = (content_summary or "") + " " + (page_title or "")
        if not combined.strip():
            return 0.0

        combined_lower = combined.lower()
        keywords = [
            "ordinance",
            "permit",
            "regulation",
            "tariff",
            "rate",
            "schedule",
            "code",
            "document",
        ]

        matching = sum(1 for kw in keywords if kw in combined_lower)
        # Moderate score for content keywords
        return min(1.0, matching * 0.15)

    @staticmethod
    def _compute_trust_signal(url: str) -> float:
        """Compute trust signal based on URL characteristics (0.0 - 1.0).

        Rewards:
        - Government domains (.gov, .ca, etc.)
        - Known document repositories
        Penalizes:
        - Unknown/suspicious domains
        - Short/generic URLs
        """
        score = 0.5  # Base neutral score

        url_lower = url.lower()

        # Reward government domains
        if any(domain in url_lower for domain in [".gov", ".ca", ".us", ".org"]):
            score += 0.2

        # Reward known document hosting patterns
        if any(pattern in url_lower for pattern in ["cms2.revize.com", "content."]):
            score += 0.1

        # Small penalty for shortened/obfuscated URLs
        if any(shortener in url_lower for shortener in ["bit.ly", "tinyurl", "goo.gl"]):
            score -= 0.15

        return min(1.0, max(0.0, score))

    def validate(self, validation_input: ValidationInput) -> ValidationResult:
        """Validate a candidate and compute signals/assignment.

        Returns:
            ValidationResult with validity, acceptance class, and reasoning.
        """
        url = validation_input.candidate_url.strip()
        reasons: list[str] = []

        # Extract extension from URL
        extension = self._extract_extension_from_url(url)
        if not extension:
            # Try to infer from anchor text
            if validation_input.anchor_text:
                for ext in self.SUPPORTED_EXTENSIONS:
                    if ext in validation_input.anchor_text.lower():
                        extension = ext
                        break

        # Check if extension is supported
        is_valid_extension = extension and extension.lower() in self.SUPPORTED_EXTENSIONS
        if extension and not is_valid_extension:
            reasons.append(f"Unsupported file extension: {extension}")
            if extension.lower() in self.UNSUPPORTED_EXTENSIONS:
                reasons.append(f"Extension {extension} is explicitly unsupported for extraction")

        # Compute canonical URL
        canonical_url = self._compute_canonical_url(url)
        reasons.append(f"Canonical URL: {canonical_url}")

        # Compute signals
        url_signal = self._compute_url_signal(url, extension)
        anchor_signal = self._compute_anchor_signal(validation_input.anchor_text)
        content_signal = self._compute_content_signal(
            validation_input.content_summary, validation_input.page_title
        )
        trust_signal = self._compute_trust_signal(url)

        # Create score and get acceptance class
        score = CandidateScore(
            url_signal=url_signal,
            anchor_signal=anchor_signal,
            content_signal=content_signal,
            trust_signal=trust_signal,
        )
        acceptance_class = score.acceptance_class()

        # Overall validity: has some positive signals
        total_score = score.weighted_total()
        is_valid = total_score >= 0.30  # Minimum signal threshold

        if not is_valid:
            reasons.append(f"Signal score {total_score:.3f} below minimum threshold (0.30)")

        return ValidationResult(
            is_valid=is_valid,
            acceptance_class=acceptance_class,
            reasons=reasons,
            mime_type=None,  # Would be populated with actual content type
            extension=extension,
            canonical_url=canonical_url,
            content_hash=None,  # Would be populated with actual content
            signals={
                "url_signal": url_signal,
                "anchor_signal": anchor_signal,
                "content_signal": content_signal,
                "trust_signal": trust_signal,
                "total": total_score,
            },
        )

    @staticmethod
    def to_candidate(
        validation_input: ValidationInput,
        validation_result: ValidationResult,
        content_for_hash: str = "",
    ) -> AcquisitionCandidate:
        """Convert validation input/result to AcquisitionCandidate model."""
        content_hash = None
        if content_for_hash:
            content_hash = CandidateValidator._compute_content_hash(content_for_hash)

        score = CandidateScore(
            url_signal=validation_result.signals.get("url_signal", 0.0)
            if validation_result.signals
            else 0.0,
            anchor_signal=validation_result.signals.get("anchor_signal", 0.0)
            if validation_result.signals
            else 0.0,
            content_signal=validation_result.signals.get("content_signal", 0.0)
            if validation_result.signals
            else 0.0,
            trust_signal=validation_result.signals.get("trust_signal", 0.0)
            if validation_result.signals
            else 0.0,
        )

        return AcquisitionCandidate(
            url=validation_input.candidate_url,
            source=validation_input.source,
            score=score,
            reasons=validation_result.reasons,
            status=validation_result.acceptance_class if not validation_result.is_valid else None,
            mime_type=validation_result.mime_type,
            extension=validation_result.extension,
            canonical_url=validation_result.canonical_url,
            content_hash=content_hash,
        )


class CandidateDeduplicator:
    """Deduplicates candidates by canonical URL and content hash."""

    @staticmethod
    def deduplicate(
        candidates: list[AcquisitionCandidate],
        prefer_accepted: bool = True,
    ) -> list[AcquisitionCandidate]:
        """Deduplicate candidates, keeping best representative.

        Deduplication strategy:
        1. Group by canonical URL
        2. Within groups, prefer 'accepted' status if prefer_accepted=True
        3. Within status, prefer higher scoring candidates
        4. Return deduplicated list

        Args:
            candidates: List of candidates to deduplicate
            prefer_accepted: If True, prefer 'accepted' status candidates

        Returns:
            Deduplicated list of candidates
        """
        if not candidates:
            return []

        # Group by canonical URL
        groups: dict[str, list[AcquisitionCandidate]] = {}
        for candidate in candidates:
            key = candidate.canonical_url or candidate.url
            if key not in groups:
                groups[key] = []
            groups[key].append(candidate)

        # Select best representative from each group
        deduplicated: list[AcquisitionCandidate] = []
        for group in groups.values():
            if not group:
                continue

            # Sort by preference
            def sort_key(c: AcquisitionCandidate) -> tuple:
                # Tuple: (prefer_accepted, score_descending, source_priority)
                status_priority = (
                    0 if (c.score.acceptance_class() == "accepted" and prefer_accepted) else 1
                )
                score = -(c.score.weighted_total())  # Negative for descending sort
                source_priority = 0 if c.source == "seed_url" else 1  # Prefer seeds
                return (status_priority, score, source_priority)

            best = min(group, key=sort_key)
            deduplicated.append(best)

        return deduplicated

    @staticmethod
    def deduplicate_by_content_hash(
        candidates: list[AcquisitionCandidate],
    ) -> list[AcquisitionCandidate]:
        """Deduplicate candidates by content hash.

        Only applies to candidates that have computed content_hash.
        Groups identical content under different URLs.
        """
        if not candidates:
            return []

        # Separate candidates with/without content hash
        with_hash = [c for c in candidates if c.content_hash]
        without_hash = [c for c in candidates if not c.content_hash]

        # Deduplicate with_hash by content_hash
        hash_groups: dict[str, list[AcquisitionCandidate]] = {}
        for candidate in with_hash:
            h = candidate.content_hash
            if h not in hash_groups:
                hash_groups[h] = []
            hash_groups[h].append(candidate)

        # Keep best from each hash group
        deduped_by_hash: list[AcquisitionCandidate] = []
        for group in hash_groups.values():
            if group:
                best = max(group, key=lambda c: c.score.weighted_total())
                deduped_by_hash.append(best)

        # Combine with candidates that have no hash (can't dedupe)
        return deduped_by_hash + without_hash


@dataclass(slots=True)
class ContentSamplingResult:
    """Result of content sampling validation."""

    success: bool
    text_extracted: str | None
    keywords_found: list[str]
    keywords_missing: list[str]
    confidence_score: float
    sample_length: int
    reasons: list[str]
    error: str | None = None


class ContentSampler:
    """Samples and validates content from downloaded files for keyword presence.

    Supports:
    - PDF: uses pdftotext or PyMuPDF
    - DOCX/DOC: uses python-docx
    - XLSX: uses openpyxl
    - CSV: direct extraction
    - TXT: direct read
    """

    MAX_SAMPLE_CHARS = 10000  # Sample first N chars
    MIN_EXTRACTION_LENGTH = 50  # Require at least 50 chars of meaningful text

    @staticmethod
    def _extract_text_from_pdf(file_path: str) -> str:
        """Extract text from PDF file."""
        try:
            import pdftotext

            with open(file_path, "rb") as f:
                pdf = pdftotext.PDF(f)
                # Extract first N pages to limit context
                max_pages = min(5, len(pdf))
                text = "\n".join(pdf[:max_pages])
                return text
        except Exception as e:
            # Fallback to PyMuPDF if pdftotext fails
            try:
                import fitz

                doc = fitz.open(file_path)
                text = ""
                for page_num in range(min(5, len(doc))):
                    page = doc[page_num]
                    text += page.get_text() + "\n"
                return text
            except Exception as fallback_e:
                raise ValueError(f"Failed to extract PDF text: {e}, fallback error: {fallback_e}")

    @staticmethod
    def _extract_text_from_docx(file_path: str) -> str:
        """Extract text from DOCX file."""
        try:
            from docx import Document

            doc = Document(file_path)
            text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
            return text
        except Exception as e:
            raise ValueError(f"Failed to extract DOCX text: {e}")

    @staticmethod
    def _extract_text_from_doc(file_path: str) -> str:
        """Extract text from DOC file (legacy Word).

        Note: python-docx doesn't support legacy .doc files.
        This requires python-docx[oxml] or external converter.
        """
        try:
            # Try using LibreOffice converter or fallback message
            import subprocess

            result = subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "docx", file_path],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Converted to DOCX, extract from that
                docx_path = file_path.replace(".doc", ".docx")
                if Path(docx_path).exists():
                    return ContentSampler._extract_text_from_docx(docx_path)

            raise ValueError("Could not convert DOC to DOCX")
        except Exception as e:
            raise ValueError(f"Failed to extract DOC text: {e}")

    @staticmethod
    def _extract_text_from_xlsx(file_path: str) -> str:
        """Extract text from XLSX file."""
        try:
            from openpyxl import load_workbook

            wb = load_workbook(file_path, data_only=True)
            text_parts = []

            for sheet_name in wb.sheetnames[:3]:  # First 3 sheets
                ws = wb[sheet_name]
                text_parts.append(f"Sheet: {sheet_name}")
                for row in ws.iter_rows(values_only=True):
                    # Convert row values to strings, skipping None
                    cells = [str(cell) if cell is not None else "" for cell in row]
                    text_parts.append(" ".join(cells))

            return "\n".join(text_parts)
        except Exception as e:
            raise ValueError(f"Failed to extract XLSX text: {e}")

    @staticmethod
    def _extract_text_from_csv(file_path: str) -> str:
        """Extract text from CSV file."""
        try:
            import csv

            text_parts = []
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                for row in reader:
                    text_parts.append(" ".join(row))
            return "\n".join(text_parts)
        except Exception as e:
            raise ValueError(f"Failed to extract CSV text: {e}")

    @staticmethod
    def _extract_text_from_txt(file_path: str) -> str:
        """Extract text from TXT file."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception as e:
            raise ValueError(f"Failed to extract TXT text: {e}")

    @classmethod
    def extract_text(cls, file_path: str) -> str:
        """Extract text from file based on extension.

        Args:
            file_path: Path to the file

        Returns:
            Extracted text

        Raises:
            ValueError: If extraction fails or file type is unsupported
        """
        file_path_lower = file_path.lower()

        if file_path_lower.endswith(".pdf"):
            return cls._extract_text_from_pdf(file_path)
        elif file_path_lower.endswith(".docx"):
            return cls._extract_text_from_docx(file_path)
        elif file_path_lower.endswith(".doc"):
            return cls._extract_text_from_doc(file_path)
        elif file_path_lower.endswith(".xlsx"):
            return cls._extract_text_from_xlsx(file_path)
        elif file_path_lower.endswith(".csv"):
            return cls._extract_text_from_csv(file_path)
        elif file_path_lower.endswith(".txt"):
            return cls._extract_text_from_txt(file_path)
        else:
            raise ValueError(f"Unsupported file type for content sampling: {file_path}")

    @classmethod
    def validate_content(
        cls,
        file_path: str,
        required_keywords: list[str] | None = None,
        nice_to_have_keywords: list[str] | None = None,
        min_required_matches: int = 1,
    ) -> ContentSamplingResult:
        """Validate content of a file by checking for keywords.

        Args:
            file_path: Path to the file to validate
            required_keywords: List of keywords that must be present
            nice_to_have_keywords: List of keywords that improve score
            min_required_matches: Minimum required keyword matches to pass

        Returns:
            ContentSamplingResult with validation details
        """
        reasons: list[str] = []
        error = None
        text_extracted = None
        keywords_found: list[str] = []
        keywords_missing: list[str] = []
        confidence_score = 0.0
        sample_length = 0

        # Default to lower-case keywords
        required_keywords = [kw.lower() for kw in (required_keywords or [])]
        nice_to_have_keywords = [kw.lower() for kw in (nice_to_have_keywords or [])]

        try:
            # Extract text
            text_extracted = cls.extract_text(file_path)
            text_lower = text_extracted.lower()
            sample_length = len(text_extracted)

            reasons.append(f"Extracted {sample_length} characters from {Path(file_path).name}")

            # Check if extraction yielded meaningful content
            if sample_length < cls.MIN_EXTRACTION_LENGTH:
                reasons.append(
                    f"Extracted text too short ({sample_length} < {cls.MIN_EXTRACTION_LENGTH})"
                )
                return ContentSamplingResult(
                    success=False,
                    text_extracted=None,
                    keywords_found=keywords_found,
                    keywords_missing=keywords_found,
                    confidence_score=0.0,
                    sample_length=sample_length,
                    reasons=reasons,
                    error="Insufficient content extracted",
                )

            # Limit sample for analysis
            text_sample = text_lower[: cls.MAX_SAMPLE_CHARS]

            # Check required keywords
            for kw in required_keywords:
                if kw in text_sample:
                    keywords_found.append(kw)
                else:
                    keywords_missing.append(kw)

            # Check nice-to-have keywords
            nice_keywords_found = []
            for kw in nice_to_have_keywords:
                if kw in text_sample:
                    nice_keywords_found.append(kw)

            # Compute confidence score
            total_keywords = len(required_keywords) + len(nice_to_have_keywords)
            required_matches = len(keywords_found)
            nice_matches = len(nice_keywords_found)

            if required_keywords:
                required_ratio = required_matches / len(required_keywords)
            else:
                required_ratio = 1.0  # Pass if no required keywords specified

            if nice_to_have_keywords:
                nice_ratio = nice_matches / len(nice_to_have_keywords)
            else:
                nice_ratio = 1.0

            # Compute overall score: 70% required, 30% nice-to-have
            confidence_score = (required_ratio * 0.7) + (nice_ratio * 0.3)

            # Determine pass/fail
            # If no required keywords, automatically pass (success=True)
            # Otherwise, success requires achieving min_required_matches
            if not required_keywords:
                success = True
            else:
                success = required_matches >= min_required_matches

            if success:
                if required_keywords:
                    reasons.append(
                        f"Found {required_matches}/{len(required_keywords)} required keywords"
                    )
                else:
                    reasons.append("No required keywords specified (validation passed)")
                if nice_to_have_keywords:
                    reasons.append(
                        f"Found {nice_matches}/{len(nice_to_have_keywords)} nice-to-have keywords"
                    )
            else:
                reasons.append(
                    f"Only found {required_matches}/{len(required_keywords)} required keywords"
                )
                if keywords_missing:
                    reasons.append(f"Missing keywords: {', '.join(keywords_missing)}")

            return ContentSamplingResult(
                success=success,
                text_extracted=text_extracted[: cls.MAX_SAMPLE_CHARS],
                keywords_found=keywords_found,
                keywords_missing=keywords_missing,
                confidence_score=confidence_score,
                sample_length=sample_length,
                reasons=reasons,
                error=None,
            )

        except Exception as e:
            error = str(e)
            reasons.append(f"Error during content sampling: {error}")
            return ContentSamplingResult(
                success=False,
                text_extracted=None,
                keywords_found=keywords_found,
                keywords_missing=required_keywords or [],
                confidence_score=0.0,
                sample_length=sample_length,
                reasons=reasons,
                error=error,
            )
