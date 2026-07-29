"""ParseSweep typed exception hierarchy.

All public-facing errors raised by the engines and pipeline layer are
subclasses of :class:`ParseSweepError`. The CLI catches them and renders
them via Rich; the programmatic API lets them propagate naturally.

Usage:
    from psweep.exceptions import ExtractionError, ConfigurationError
"""

from __future__ import annotations


class ParseSweepError(Exception):
    """Base exception for all ParseSweep errors."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        if self.hint:
            return f"{base}\n\nHint: {self.hint}"
        return base


class ConfigurationError(ParseSweepError):
    """Raised when a run.yaml or schema cannot be loaded or validated."""


class SchemaError(ParseSweepError):
    """Raised when a schema file is missing, malformed, or fails $metadata validation."""


class ExtractionError(ParseSweepError):
    """Raised when document extraction fails at the engine level."""

    def __init__(
        self,
        message: str,
        *,
        document: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.document = document


class CompilationError(ParseSweepError):
    """Raised when JSON→Excel/CSV compilation fails."""


class DiscoveryError(ParseSweepError):
    """Raised when web document discovery fails."""


class PipelineError(ParseSweepError):
    """Raised when a multi-stage pipeline run fails.

    Carries the failed stage name for structured error reporting.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.stage = stage


class APIKeyError(ConfigurationError):
    """Raised when no valid API credentials are found in the environment."""

    def __init__(self) -> None:
        super().__init__(
            "No API credentials found.",
            hint=(
                "Set AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT (Azure) "
                "or OPENAI_API_KEY (OpenAI) in your .env file, "
                "then run: psweep init"
            ),
        )
