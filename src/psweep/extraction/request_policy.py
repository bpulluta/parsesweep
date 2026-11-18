"""Pure request admission: does the exact request fit the route's known limits?

No network and no model-name tables: limits come from the operator's
``model_context_windows`` and LiteLLM's pinned local catalog. Estimates are
deliberately conservative; an unknown limit stays unknown and is never
treated as a large one.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from dataclasses import dataclass
from typing import Any

import litellm

from psweep.exceptions import AttemptBudgetExceeded, ExtractionError

# English averages ~4 characters per token; dense or non-Latin text can
# approach one token per UTF-8 character. Three bytes per token is safe for
# both without a tokenizer dependency per provider.
BYTES_PER_TOKEN = 3
PER_MESSAGE_OVERHEAD_TOKENS = 16


# Pixels per token for image input; the smallest figure any major provider
# documents, so the estimate errs high. Each image also pays a fixed overhead.
PIXELS_PER_TOKEN = 750
IMAGE_OVERHEAD_TOKENS = 256
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DATA_URI_PREFIX = "data:image/png;base64,"


class RequestNotAdmittedError(ExtractionError):
    """The request cannot fit the route's known limits; nothing was sent."""


class AttemptAllowance:
    """Physical provider attempts one document may still send.

    Passed as ``before_request`` for every request of that document, so the
    original generation, a feedback generation, transport retries and the
    temperature fallback all draw from one count. It is created per document
    and never stored on a shared client.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        """Attempts still available to this document."""
        return self.limit - self.used

    def reserve(self) -> None:
        """Reserve one attempt immediately before it is sent, or send nothing."""
        if self.remaining <= 0:
            raise AttemptBudgetExceeded("Extraction attempt allowance exhausted for document")
        self.used += 1


@dataclass(frozen=True)
class RouteLimits:
    """Known limits for one route; ``None`` means unknown, not unlimited."""

    input_tokens: int | None = None
    output_tokens: int | None = None


def route_limits(
    *model_names: str, configured_windows: dict[str, int] | None = None
) -> RouteLimits:
    """Resolve limits: operator windows win, then the pinned local catalog."""
    windows = configured_windows or {}
    input_tokens = next((windows[name] for name in model_names if name in windows), None)
    catalog: dict[str, Any] = {}
    for name in model_names:
        try:
            catalog = litellm.get_model_info(name) or {}
        except Exception:  # noqa: BLE001, S112 - unmapped route: limits stay unknown
            continue
        break
    if input_tokens is None:
        input_tokens = catalog.get("max_input_tokens")
    output_tokens = catalog.get("max_output_tokens")
    return RouteLimits(
        input_tokens=input_tokens if isinstance(input_tokens, int) and input_tokens > 0 else None,
        output_tokens=(
            output_tokens if isinstance(output_tokens, int) and output_tokens > 0 else None
        ),
    )


def image_block(image) -> dict[str, Any]:
    """OpenAI-style inline image content block for a rendered page."""
    encoded = base64.b64encode(image.png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": _DATA_URI_PREFIX + encoded}}


def _image_tokens(block: dict[str, Any]) -> int:
    """Conservative tokens for an inline PNG, read from its own header."""
    url = (block.get("image_url") or {}).get("url", "")
    try:
        header = (
            base64.b64decode(url[len(_DATA_URI_PREFIX) :][:44])
            if url.startswith(_DATA_URI_PREFIX)
            else b""
        )
        if header[:8] != _PNG_SIGNATURE:
            raise ValueError("not a PNG data URI")
        width, height = struct.unpack(">II", header[16:24])
    except (ValueError, struct.error):
        message = "request_not_admitted: image dimensions are unknown, so its size is unknown"
        raise RequestNotAdmittedError(message) from None
    return math.ceil(width * height / PIXELS_PER_TOKEN) + IMAGE_OVERHEAD_TOKENS


def estimate_text_tokens(text: str) -> int:
    """Conservative upper estimate of text tokens."""
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN)


def estimate_request_tokens(
    messages: list[dict[str, Any]], response_format: dict[str, Any] | None = None
) -> int:
    """Estimate input tokens of the final request: messages plus response schema."""
    total = PER_MESSAGE_OVERHEAD_TOKENS * len(messages)
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        else:
            for block in content:
                if block.get("type") == "text":
                    total += estimate_text_tokens(block["text"])
                elif block.get("type") == "image_url":
                    total += _image_tokens(block)
                else:  # an unknown block type has no estimate: never guess it small
                    kind = block.get("type")
                    raise RequestNotAdmittedError(
                        f"request_not_admitted: unsupported content block {kind!r}"
                    )
    if response_format:
        total += estimate_text_tokens(json.dumps(response_format, sort_keys=True))
    return total


def assess_request(
    messages: list[dict[str, Any]],
    response_format: dict[str, Any] | None,
    limits: RouteLimits,
    *,
    output_cap: int | None = None,
) -> int:
    """Raise ``RequestNotAdmittedError`` if the request exceeds a known limit.

    Returns the estimated input tokens. ``output_cap`` is the maximum output
    the caller will allow; it must also fit the route's output limit.
    """
    estimated = estimate_request_tokens(messages, response_format)
    if limits.input_tokens is not None and estimated > limits.input_tokens:
        raise RequestNotAdmittedError(
            f"context_window_exceeded: estimated request size {estimated} input tokens "
            f"exceeds the route input limit {limits.input_tokens}. "
            "Reduce max_context_chars or use page ranges/targeting for large documents."
        )
    if (
        output_cap is not None
        and limits.output_tokens is not None
        and output_cap > limits.output_tokens
    ):
        raise RequestNotAdmittedError(
            f"request_not_admitted: output cap {output_cap} exceeds the route output "
            f"limit {limits.output_tokens}"
        )
    return estimated
