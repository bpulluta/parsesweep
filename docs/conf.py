"""Sphinx configuration for the ParseSweep documentation.

The API reference is generated statically from source by ``sphinx-autoapi``
(no import side effects), while the CLI reference is rendered by
``sphinx-click`` from the resolved Click command group. Prose pages are
authored in Markdown and parsed by ``myst-parser``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable for sphinx-click, which introspects the live
# Click command group. (autoapi parses source and needs no import.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from psweep import __version__  # noqa: E402

# -- Project information ------------------------------------------------------
project = "ParseSweep"
author = "Byron Pullutasig, NLR"
copyright = "2026, Byron Pullutasig"  # noqa: A001
version = __version__
release = __version__

# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.napoleon",  # render NumPy-style docstrings (autoapi respects it)
    "autoapi.extension",
    "sphinx_click",
    "myst_parser",
    "sphinx_copybutton",
]

# NumPy docstring convention (matches ruff's pydocstyle config).
napoleon_numpy_docstring = True
napoleon_google_docstring = False
# Render class "Attributes" sections as inline :ivar: fields so they don't
# collide with autoapi's own per-attribute object descriptions (dataclasses).
napoleon_use_ivar = True

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- MyST (Markdown) ---------------------------------------------------------
myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

# -- AutoAPI -----------------------------------------------------------------
autoapi_dirs = ["../src/psweep"]
# The CLI is documented by sphinx-click (see commands/index.md), which renders
# every command, option, and default from the live Click objects. Excluding the
# cli package from autoapi avoids duplicating that coverage and sidesteps the
# Click `\b`/EXAMPLES help idioms that are not valid reStructuredText.
autoapi_ignore = ["*/cli/*"]
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    # "imported-members" intentionally omitted: re-exports in __init__.py would
    # be documented both here and in their defining module, producing duplicate
    # object descriptions and ambiguous cross-references. Each symbol is
    # documented once, in the module that defines it.
]
autoapi_python_class_content = "both"
autoapi_member_order = "groupwise"

# -- HTML output -------------------------------------------------------------
html_theme = "furo"
html_title = f"ParseSweep {release}"
