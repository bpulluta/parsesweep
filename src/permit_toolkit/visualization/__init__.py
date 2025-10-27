"""Visualization tools for permit data."""

# Optional import - only required for map generation
try:
    from .mapper import FacilityMapper
    __all__ = ['FacilityMapper']
except ImportError:
    # Dependencies not installed
    FacilityMapper = None
    __all__ = []

