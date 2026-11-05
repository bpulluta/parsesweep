"""Turn resolved discovery inputs into ``DiscoveryRequest`` fields.

Config values are validated by ``psweep.config.discovery_config``; this layer
selects request fields, materializes targets after ``--target-limit``, checks
CLI-only values, and rejects plans with nothing to discover. The CLI and the
pipeline preflight share it.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from .engine import DiscoveryRequest
from .targets import TargetSpecError, iter_targets
from .urls import is_http_url

_REQUEST_FIELDS = frozenset(f.name for f in fields(DiscoveryRequest)) - {"progress_callback"}


def discovery_request_fields(resolved: dict[str, Any]) -> dict[str, Any]:
    """Return ``DiscoveryRequest`` keyword arguments or raise ``ValueError``."""
    values = {k: v for k, v in resolved.items() if k in _REQUEST_FIELDS}
    if not values.get("domain"):
        raise ValueError("Discovery needs a domain: pass --config or --domain")
    values["sources"] = [url.strip() for url in values.get("sources") or ()]
    values["queries"] = list(values.get("queries") or ())
    invalid = [url for url in values["sources"] if not is_http_url(url)]
    if invalid:
        raise ValueError(f"--source must be an http(s) URL (got {invalid[0]!r})")
    spec = resolved.get("targets")
    try:
        targets = list(iter_targets(spec, resolved.get("target_limit"))) if spec is not None else []
    except TargetSpecError as exc:
        raise ValueError(str(exc)) from exc
    values["targets"] = targets or None
    if not (
        values["sources"]
        or values["queries"]
        or any(row.get("query") or row.get("urls") for row in targets)
    ):
        raise ValueError(
            "Discovery has nothing to do: set discovery.sources, "
            "discovery.queries, or target rows with query/urls "
            "(or pass --source/--query)"
        )
    return values
