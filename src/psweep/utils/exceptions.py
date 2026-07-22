"""Custom exceptions for ParseSweep."""


class PsweepError(Exception):
    """Base exception for all ParseSweep errors."""

    pass


class SchemaMetadataError(PsweepError):
    """Raised when schema metadata is missing or invalid."""

    def __init__(self, message: str, schema_path: str = None):
        """
        Initialize SchemaMetadataError.

        Args:
            message: Error description
            schema_path: Optional path to the schema file
        """
        self.schema_path = schema_path

        if schema_path:
            full_message = f"{message}\n\nSchema file: {schema_path}"
        else:
            full_message = message

        super().__init__(full_message)


class SchemaValidationError(PsweepError):
    """Raised when schema structure is invalid."""

    pass


class ExtractionError(PsweepError):
    """Raised when document extraction fails."""

    pass


class ConsolidationError(PsweepError):
    """Raised when data consolidation fails."""

    pass
