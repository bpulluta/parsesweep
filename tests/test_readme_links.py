"""The README's relative links must point at files that exist in the repository."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_readme_relative_links_resolve() -> None:
    readme = ROOT / "README.md"
    if not readme.is_file():
        return  # not a source checkout (for example an installed wheel)
    targets = []
    for raw in LINK.findall(readme.read_text(encoding="utf-8")):
        if re.match(r"^[a-z][a-z0-9+.-]*:", raw) or raw.startswith("#"):
            continue
        targets.append(raw.split("#", 1)[0])
    assert targets, "README has no relative links to check"
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing, f"README links to missing paths: {missing}"
