"""Acquisition runtime scaffold for web document discovery."""

from .engine import (
    AcquisitionEngine,
    AcquisitionRequest,
    AcquisitionResult,
)
from .models import (
    AcquisitionCandidate,
    AcquisitionManifest,
    CandidateScore,
)

__all__ = [
    "AcquisitionEngine",
    "AcquisitionRequest",
    "AcquisitionResult",
    "AcquisitionCandidate",
    "AcquisitionManifest",
    "CandidateScore",
]
