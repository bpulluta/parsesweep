"""Package version from installed metadata (importable without loading the package API)."""

from importlib.metadata import version

__version__ = version("psweep")
