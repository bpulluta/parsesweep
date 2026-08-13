"""Discovery runtime scaffold for web document discovery."""

from .engine import (
    DEFAULT_PARTITION_MODE,
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
    "DEFAULT_PARTITION_MODE",
    "DEFAULT_ROBOTS_POLICY_MODE",
    "DEFAULT_TOS_POLICY_MODE",
    "DiscoveryEngine",
    "DiscoveryRequest",
    "DiscoveryResult",
    "DiscoveryCandidate",
    "DiscoveryManifest",
    "CandidateScore",
]
