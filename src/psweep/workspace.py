"""Domain workspace layout: the single source of truth for input and output paths.

Inputs live in ``config/<domain>/`` (``run.yaml``, ``schema.json``, optional
``targets.csv``); every stage output is derived from the domain under
``outputs/<domain>/`` relative to the working directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONFIG_ROOT = Path("config")
OUTPUTS_ROOT = Path("outputs")
SCHEMA_FILENAME = "schema.json"
DISCOVERED_DIRNAME = "discovered"


@dataclass(frozen=True)
class Workspace:
    """Derived stage directories for one domain."""

    domain: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.domain, str)
            or self.domain in {"", ".", ".."}
            or Path(self.domain).name != self.domain
            or "\\" in self.domain
        ):
            msg = f"Invalid domain {self.domain!r}: must be a single directory name"
            raise ValueError(msg)

    @property
    def root(self) -> Path:
        return OUTPUTS_ROOT / self.domain

    @property
    def discovered(self) -> Path:
        return self.root / DISCOVERED_DIRNAME

    @property
    def curated(self) -> Path:
        """Consolidated curated corpus; the default extraction input."""
        return self.discovered / "curated"

    @property
    def serpapi_cache(self) -> Path:
        """Domain-level SerpApi response cache shared by all runs."""
        return self.discovered / ".serpapi_cache"

    @property
    def review_cache(self) -> Path:
        """Domain-level, content-addressed document grade cache."""
        return self.discovered / ".review_cache"

    @property
    def latest(self) -> Path:
        """Internal pointer to the most recent discovery run."""
        return self.discovered / "latest"

    @property
    def extracted(self) -> Path:
        return self.root / "extracted"

    @property
    def validated(self) -> Path:
        return self.root / "validated"

    @property
    def compiled(self) -> Path:
        return self.root / "compiled"

    @property
    def compiled_name(self) -> str:
        """Base name of the compiled CSV/XLSX (``<domain>.csv``); the one place it is derived."""
        return self.domain


def default_schema_path(config_path: Path) -> Path:
    """Schema expected beside a run config when ``schema:`` is omitted."""
    return Path(config_path).parent / SCHEMA_FILENAME


def domain_schemas(config_root: Path = CONFIG_ROOT) -> list[Path]:
    """All domain schemas under ``config_root`` (``<domain>/schema.json``)."""
    return sorted(Path(config_root).glob(f"*/{SCHEMA_FILENAME}"))


def infer_domain(path: Path) -> str:
    """Domain for a config-less run: ``outputs/<domain>/...`` or the input's directory name."""
    path = Path(path)
    resolved = path.resolve()
    outputs = OUTPUTS_ROOT.resolve()
    if resolved != outputs and resolved.is_relative_to(outputs):
        return resolved.relative_to(outputs).parts[0]
    return (resolved if path.is_dir() else resolved.parent).name
