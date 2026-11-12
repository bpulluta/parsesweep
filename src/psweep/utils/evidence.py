"""Pure source-quote normalization shared by discovery, extraction and validation."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from collections.abc import Iterable
from typing import Any

_PUNCTUATION = re.compile(r"[^\w\s]|_")
_LINE_HYPHEN = re.compile(r"([^\W\d_])[-‐‑]\s*\n\s*(?=[^\W\d_])")  # noqa: RUF001 - PDF hyphens
_ELLIPSIS = re.compile(r"…|\.{3}")


def normalize(text: Any) -> str:
    """NFKD, remove marks/soft hyphens, join line hyphenation, normalize punctuation."""
    text = _LINE_HYPHEN.sub(r"\1", str(text).replace("­", ""))
    text = unicodedata.normalize("NFKD", text)
    if not text.isascii():
        text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


@lru_cache(maxsize=8)
def _haystacks(source_text: str) -> frozenset[str]:
    return frozenset(
        f" {normalize(text)} " for text in (source_text, _LINE_HYPHEN.sub(r"\1 ", source_text))
    )


def _in_order(parts: list[str], haystack: str) -> bool:
    start = 0
    for part in parts:
        start = haystack.find(f" {part} ", start)
        if start < 0:
            return False
        start += len(part) + 1
    return True


def quote_present(quote: Any, source_text: str) -> bool:
    """Match on normalized word boundaries; ellipsis fragments must occur in order."""
    if quote is None:
        return False
    parts = [part for part in map(normalize, _ELLIPSIS.split(str(quote))) if part]
    return bool(parts) and any(_in_order(parts, haystack) for haystack in _haystacks(source_text))


def quote_in_segments(quote: Any, segments: Iterable[str]) -> bool:
    """Match within one real sampled segment; never bridge disconnected windows."""
    return any(quote_present(quote, segment) for segment in segments)
