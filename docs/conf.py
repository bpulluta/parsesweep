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

# The embedded schema guide contains illustrative JSON snippets with ``...``
# placeholders and ``//`` comments that are not strictly valid JSON. Tolerate
# the resulting Pygments lexing failures so the ``-W`` build still fails loudly
# on the warnings that matter (broken refs, missing toctree entries, etc.).
suppress_warnings = ["misc.highlighting_failure"]

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
    "show-inheritance",
    "show-module-summary",
    # "undoc-members" intentionally omitted: Phase 2 documented the whole public
    # API (0 pydocstyle violations), so this only pulled truly-undocumented
    # internal symbols into the sidebar, flooding the nav. Documented members
    # still render; the reference stays focused on the public surface.
    # "imported-members" intentionally omitted: re-exports in __init__.py would
    # be documented both here and in their defining module, producing duplicate
    # object descriptions and ambiguous cross-references. Each symbol is
    # documented once, in the module that defines it.
]
autoapi_python_class_content = "both"
autoapi_member_order = "groupwise"

# -- Copy button -------------------------------------------------------------
# Strip a leading "$ " shell prompt so copied commands paste cleanly.
copybutton_prompt_text = r"\$ "
copybutton_prompt_is_regexp = True

# -- HTML output -------------------------------------------------------------
html_theme = "furo"
html_title = f"ParseSweep {release}"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_favicon = "_static/favicon.svg"

_REPO_URL = "https://github.com/bpulluta/parsesweep"
html_theme_options = {
    # Teal brand — evokes structure/precision; AA/AAA contrast in both themes.
    "light_css_variables": {
        "color-brand-primary": "#0f766e",  # teal-700, ~4.9:1 on white (AA)
        "color-brand-content": "#0e7490",  # cyan-700, ~4.8:1 on white (AA)
    },
    "dark_css_variables": {
        "color-brand-primary": "#5eead4",  # teal-300, ~11:1 on furo dark (AAA)
        "color-brand-content": "#2dd4bf",  # teal-400, ~8:1 on furo dark (AAA)
    },
    # "Edit this page" / view-source links back to the repo.
    "source_repository": _REPO_URL,
    "source_branch": "main",
    "source_directory": "docs/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": _REPO_URL,
            "html": (
                '<svg stroke="currentColor" fill="currentColor" '
                'stroke-width="0" viewBox="0 0 16 16" width="1em" height="1em">'
                '<path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 '
                "2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 "
                "0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-"
                ".82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 "
                "1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-"
                "3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 "
                "0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 "
                ".27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 "
                "2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 "
                "3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 "
                '0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>'
                "</svg>"
            ),
            "class": "",
        },
    ],
}
