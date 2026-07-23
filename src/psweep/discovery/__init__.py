"""Discovery runtime scaffold for web document discovery."""

from .engine import (
    DiscoveryEngine,
    DiscoveryRequest,
    DiscoveryResult,
)
from .models import (
    DiscoveryCandidate,
    DiscoveryManifest,
    CandidateScore,
)

__all__ = [
    "DiscoveryEngine",
    "DiscoveryRequest",
    "DiscoveryResult",
    "DiscoveryCandidate",
    "DiscoveryManifest",
    "CandidateScore",
]
