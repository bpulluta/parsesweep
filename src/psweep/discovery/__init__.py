"""Configured web document discovery and curated publication."""

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
from .policies import (
    DEFAULT_ROBOTS_POLICY_MODE,
    DEFAULT_TOS_POLICY_MODE,
)

__all__ = [
    "DEFAULT_ROBOTS_POLICY_MODE",
    "DEFAULT_TOS_POLICY_MODE",
    "CandidateScore",
    "DiscoveryCandidate",
    "DiscoveryEngine",
    "DiscoveryManifest",
    "DiscoveryRequest",
    "DiscoveryResult",
]
