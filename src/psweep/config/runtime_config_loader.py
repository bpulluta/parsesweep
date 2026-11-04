"""Shared runtime config loader and resolver for CLI commands."""

from __future__ import annotations

import json
import re
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import yaml

from psweep.workspace import Workspace, default_schema_path, infer_domain

from .discovery_config import discovery_key_paths, resolve_discovery_section
from .errors import RuntimeConfigError, reject_unknown_keys
from .model_registry import ModelRegistry, ModelRegistryError


VARIABLE_CATALOG: dict[str, list[dict[str, str]]] = {
    "global": [
        {
            "name": "domain",
            "level": "optional",
            "description": (
                "Domain key; outputs are derived under outputs/<domain>/ "
                "(default: the config directory name)."
            ),
        },
        {
            "name": "schema",
            "level": "optional",
            "description": (
                "Schema path for extraction, validation, and compilation "
                "(default: schema.json beside the config file)."
            ),
        },
    ],
    "extraction": [
        {
            "name": "pages_csv",
            "level": "optional",
            "description": "CSV mapping file names to page ranges (from pages.csv or CLI --pages-csv).",
        },
        {
            "name": "pages",
            "level": "optional",
            "description": "Single-file page range (for example: 10-35, from pages.range or CLI --pages).",
        },
        {
            "name": "page_targeting",
            "level": "optional",
            "description": (
                "LLM-assisted page selection for large documents "
                "(from pages.auto_locate)."
            ),
        },
        {
            "name": "ocr_corrections",
            "level": "advanced",
            "description": (
                "Optional per-domain OCR text-correction rules "
                "({pattern, replacement, ignore_case}) applied to OCR-derived "
                "PDF text only."
            ),
        },
        {
            "name": "provider",
            "level": "optional",
            "description": (
                "LLM provider override "
                "(openai, azure, anthropic, gemini, auto)."
            ),
        },
        {
            "name": "model",
            "level": "optional",
            "description": "Model override for extraction runs.",
        },
        {
            "name": "limit",
            "level": "optional",
            "description": "Process only the first N files.",
        },
        {
            "name": "max_context",
            "level": "advanced",
            "description": "Maximum characters to pass to extraction context.",
        },
        {
            "name": "max_concurrent",
            "level": "advanced",
            "description": "Documents extracted in parallel (default: 4).",
        },
        {
            "name": "timeout_seconds",
            "level": "advanced",
            "description": (
                "Per-request LLM timeout for extraction calls (seconds). "
                "Overrides the default 120s client timeout."
            ),
        },
        {
            "name": "skip_existing",
            "level": "advanced",
            "description": "Skip files that already have output JSON.",
        },
    ],
    "compilation": [
        {
            "name": "output",
            "level": "optional",
            "description": "Output formatting: column_order, exclude_fields (output headers).",
        },
        {
            "name": "normalization",
            "level": "optional",
            "description": "Field normalization (e.g., state abbreviation).",
        },
        {
            "name": "dry_run",
            "level": "optional",
            "description": "Preview deduplication without writing files.",
        },
        {
            "name": "report_format",
            "level": "optional",
            "description": "Dry-run output format (text or json).",
        },
        {
            "name": "fail_on_suspicious",
            "level": "advanced",
            "description": (
                "Fail threshold for suspicious dedup groups in dry-run."
            ),
        },
        {
            "name": "provenance_policy",
            "level": "advanced",
            "description": (
                "How compile handles provenance warnings from extraction inputs "
                "(warn or fail)."
            ),
        },
    ],
    "validation": [
        {"name": "models", "level": "optional",
         "description": "Contender model aliases besides extraction.model."},
        {"name": "references", "level": "optional",
         "description": "Human-labelled CSV sheets (config-relative), labelled by file stem."},
        {"name": "judge", "level": "optional",
         "description": "Model alias that judges disputed cells for equivalence."},
        {"name": "fields", "level": "optional",
         "description": "Focus fields for scoring, judging and review (default: all scorable)."},
        {"name": "key_fields", "level": "optional",
         "description": "Row identity in rows mode (required there; rejected in roster mode)."},
        {"name": "quote_field", "level": "advanced",
         "description": "Rows mode: the item's verbatim quote for anchor alignment."},
        {"name": "min_accuracy", "level": "optional",
         "description": "Gate in (0, 1] on the primary's accuracy against every reference."},
    ],
    "discovery": [
        {"name": "targets", "level": "optional",
         "description": "Targets: .csv/.json path, inline list, or {cross_product: {dimensions}}."},
        {"name": "sources", "level": "optional",
         "description": "Shared document/page URLs downloaded for every run."},
        {"name": "queries", "level": "optional",
         "description": "Search query templates ({field} / {field?}); enable search."},
        {"name": "crawl", "level": "optional",
         "description": "Link following: provider, max_depth (0 = off), max_pages, allowed_domains, patterns."},
        {"name": "runtime", "level": "advanced",
         "description": "max_files, timeout_seconds, max_concurrent_downloads, min_request_interval_ms."},
        {"name": "selection", "level": "advanced",
         "description": "Per-target candidate filters and caps."},
        {"name": "document_classifier", "level": "advanced",
         "description": "Keyword checks applied to downloaded document content."},
        {"name": "document_review", "level": "advanced",
         "description": "LLM curation of downloaded documents."},
        {"name": "policy", "level": "advanced",
         "description": "robots_mode / tos_mode: ignore, warn, or enforce."},
        {"name": "retention", "level": "advanced",
         "description": "Which downloaded documents to keep (documents: all, curated, none)."},
    ],
}

_SECTION_ALIASES = {
    "discover": "discovery",
    "discovery": "discovery",
    "extract": "extraction",
    "extraction": "extraction",
    "compile": "compilation",
    "compilation": "compilation",
    "validate": "validation",
    "validation": "validation",
}

_ALLOWED_TOP_LEVEL = {
    "domain",
    "schema",
    "models",
    "model_context_windows",
    "reasoning_models",
    "discovery",
    "extraction",
    "compilation",
    "validation",
}
_ALLOWED_SECTION_FIELDS = {
    "extraction": {
        "pages",           # unified block (csv + auto_locate); normalized to pages_csv / page_targeting internally
        "pages_csv",       # internal (set by _normalize_pages_block or CLI --pages-csv)
        "page_targeting",  # internal (set by _normalize_pages_block)
        "provider",
        "model",
        "limit",
        "skip_existing",
        "max_context",
        "max_concurrent",
        "timeout_seconds",
        "ocr_corrections",  # optional per-domain OCR text-correction rules
    },
    "compilation": {
        "output",
        "normalization",
        "severity_tokens",
        "dry_run",
        "report_format",
        "fail_on_suspicious",
        "provenance_policy",
    },
    "validation": {
        "models",
        "references",
        "judge",
        "fields",
        "key_fields",
        "quote_field",
        "min_accuracy",
    },
}

_ALLOWED_PROVENANCE_POLICIES = {"warn", "fail"}

_SECTION_NAMES = ("discovery", "extraction", "compilation")
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


def _validate_models_block(models: Any) -> None:
    """Validate the optional top-level ``models`` alias map.

    Must be a mapping of non-empty alias-name strings to non-empty model-name
    strings (e.g. ``{fast: gpt-4o-mini, accurate: gpt-5}``). Alias names are
    entirely user-chosen — there are no reserved names — so a domain can define
    whatever aliases it needs and reference them from any stage's ``model:``.
    """
    if models is None:
        return
    if not isinstance(models, dict):
        raise RuntimeConfigError(
            "'models' must be a mapping of alias-name -> model-name"
        )
    for key, value in models.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise RuntimeConfigError(
                "'models' must map non-empty alias names to non-empty model "
                "names"
            )


def _validate_model_context_windows_block(windows: Any) -> None:
    """Validate the optional top-level ``model_context_windows`` map.

    Maps a model/deployment name to its context window in prompt tokens, used to
    fail fast before an over-budget extraction request. No model names are
    hardcoded anywhere; this is the only place a window is declared. Must be a
    mapping of non-empty strings to positive integers.
    """
    if windows is None:
        return
    if not isinstance(windows, dict):
        raise RuntimeConfigError(
            "'model_context_windows' must be a mapping of model-name -> "
            "max prompt tokens (positive integer)"
        )
    for key, value in windows.items():
        if not isinstance(key, str) or not key.strip():
            raise RuntimeConfigError(
                "'model_context_windows' keys must be non-empty model names"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeConfigError(
                "'model_context_windows' values must be positive integers "
                f"(got {value!r} for '{key}')"
            )


def _validate_reasoning_models_block(markers: Any) -> None:
    """Validate the optional top-level ``reasoning_models`` list.

    Extra substring markers (unioned with the built-in defaults) identifying
    reasoning models — those that reject the temperature / response_format
    params. Must be a list of non-empty strings. Only provider-neutral
    defaults live in code; anything else is declared here.
    """
    if markers is None:
        return
    if not isinstance(markers, list) or not all(
        isinstance(m, str) and m.strip() for m in markers
    ):
        raise RuntimeConfigError(
            "'reasoning_models' must be a list of non-empty model-name "
            "substring markers (e.g. ['gpt-5', 'o3'])"
        )


def _normalize_pages_block(extraction: dict[str, Any]) -> None:
    """Normalize the unified ``pages`` block into flat internal keys.

    Config format::

        pages:
          csv: path/to/ranges.csv        # manual ranges (wins)
          auto_locate:                    # LLM fallback for uncovered docs
            section_description: "..."
            trigger_chars: 200000

    Expands to internal keys ``pages_csv`` (str) and ``page_targeting``
    (dict with ``enabled: True``) consumed by the extraction pipeline.
    """
    pages = extraction.get("pages")

    if isinstance(pages, dict):
        extraction.pop("pages")
        if "csv" in pages:
            extraction["pages_csv"] = pages["csv"]
        if "auto_locate" in pages:
            al = dict(pages["auto_locate"])
            al.setdefault("enabled", True)  # presence = enabled
            extraction["page_targeting"] = al
        if "range" in pages:
            # Single-file range from config (usually a CLI thing).
            extraction["pages"] = pages["range"]
    elif "pages_csv" in extraction or "page_targeting" in extraction:
        raise RuntimeConfigError(
            "'pages_csv' and 'page_targeting' are no longer supported as "
            "top-level extraction keys. Use the unified 'pages:' block "
            "instead:\n"
            "  pages:\n"
            "    csv: path/to/ranges.csv\n"
            "    auto_locate:\n"
            "      section_description: \"...\"\n"
            "See config/TEMPLATE.yaml for the full reference."
        )


def _validate_extraction_section_schema(extraction: dict[str, Any]) -> None:
    """Deep-validate the ``extraction`` section at load time.

    Config-supplied values are checked with the same rigor as CLI flags (Click type/choice guards are
    bypassed when a value comes from YAML). Domain-neutral: only structural
    types and ranges are enforced.
    """

    def _require_positive_int(name: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeConfigError(
                f"extraction.{name} must be a positive integer (got {value!r})"
            )

    for name in ("max_context", "max_concurrent", "limit", "timeout_seconds"):
        if name in extraction and extraction[name] is not None:
            _require_positive_int(name, extraction[name])

    if extraction.get("skip_existing") is not None and not isinstance(
        extraction["skip_existing"], bool
    ):
        raise RuntimeConfigError("extraction.skip_existing must be a boolean")

    provider = extraction.get("provider")
    if provider is not None:
        allowed = {"openai", "azure", "anthropic", "gemini", "auto"}
        if not isinstance(provider, str) or provider.lower() not in allowed:
            raise RuntimeConfigError(
                "extraction.provider must be one of: "
                + ", ".join(sorted(allowed))
            )

    for name in ("pages", "pages_csv", "model"):
        if name in extraction and extraction[name] is not None:
            if not isinstance(extraction[name], str):
                raise RuntimeConfigError(
                    f"extraction.{name} must be a string"
                )

    page_targeting = extraction.get("page_targeting")
    if page_targeting is not None:
        _validate_page_targeting_block(page_targeting)

    ocr_corrections = extraction.get("ocr_corrections")
    if ocr_corrections is not None:
        _validate_ocr_corrections_block(ocr_corrections)


def _validate_ocr_corrections_block(rules: Any) -> None:
    """Validate ``extraction.ocr_corrections`` (optional OCR text-fix rules).

    A list of ``{pattern, replacement, ignore_case?}`` objects applied (in
    order) to OCR-derived PDF text. Domain-specific by nature (e.g. scanned
    air-emissions permits); absent by default so it is a no-op for every domain
    that does not opt in.
    """
    if not isinstance(rules, list):
        raise RuntimeConfigError(
            "extraction.ocr_corrections must be a list of "
            "{pattern, replacement, ignore_case?} objects"
        )
    allowed = {"pattern", "replacement", "ignore_case"}
    for i, rule in enumerate(rules):
        loc = f"extraction.ocr_corrections[{i}]"
        if not isinstance(rule, dict):
            raise RuntimeConfigError(f"{loc} must be an object")
        unknown = sorted(set(rule) - allowed)
        if unknown:
            raise RuntimeConfigError(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            )
        for key in ("pattern", "replacement"):
            if not isinstance(rule.get(key), str) or not rule.get(key):
                raise RuntimeConfigError(
                    f"{loc}.{key} must be a non-empty string"
                )
        if rule.get("ignore_case") is not None and not isinstance(
            rule["ignore_case"], bool
        ):
            raise RuntimeConfigError(f"{loc}.ignore_case must be a boolean")
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise RuntimeConfigError(
                f"{loc}.pattern is not a valid regular expression: {exc}"
            ) from exc


def _validate_page_targeting_block(pt: Any) -> None:
    """Validate the ``pages.auto_locate`` block (internal key: page_targeting)."""
    if not isinstance(pt, dict):
        raise RuntimeConfigError(
            "pages.auto_locate must be an object"
        )
    if pt.get("section_description") is not None and not isinstance(
        pt["section_description"], str
    ):
        raise RuntimeConfigError(
            "pages.auto_locate.section_description must be a string"
        )
    for name in ("trigger_chars", "max_selected_pages"):
        val = pt.get(name)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                raise RuntimeConfigError(
                    f"pages.auto_locate.{name} must be a positive "
                    f"integer (got {val!r})"
                )
    if pt.get("model") is not None and not isinstance(pt["model"], str):
        raise RuntimeConfigError(
            "pages.auto_locate.model must be a string"
        )
    if pt.get("keywords") is not None and (
        not isinstance(pt["keywords"], list)
        or any(not isinstance(keyword, str) for keyword in pt["keywords"])
    ):
        message = "pages.auto_locate.keywords must be a list of strings"
        raise RuntimeConfigError(message)
    if pt.get("save_discovered") is not None and not isinstance(
        pt["save_discovered"], bool
    ):
        raise RuntimeConfigError(
            "pages.auto_locate.save_discovered must be a boolean"
        )


def _validate_compilation_section_schema(
    compilation: dict[str, Any],
) -> None:
    """Deep-validate the ``compilation`` section at load time."""
    provenance_policy = compilation.get("provenance_policy")
    if provenance_policy is not None:
        if not isinstance(provenance_policy, str):
            raise RuntimeConfigError(
                "compilation.provenance_policy must be a string"
            )
        normalized_policy = provenance_policy.strip().lower()
        if normalized_policy not in _ALLOWED_PROVENANCE_POLICIES:
            allowed = ", ".join(sorted(_ALLOWED_PROVENANCE_POLICIES))
            raise RuntimeConfigError(
                "compilation.provenance_policy must be one of: "
                f"{allowed} (got {provenance_policy!r})"
            )
        compilation["provenance_policy"] = normalized_policy


_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CELLS_COLUMNS = frozenset(
    {"primary", "consensus", "match", "column", "status", "miss", "judge_groups",
     "judge_reason", "review_priority"}
)
_CELLS_SUFFIXES = ("_quote", "_url", "_support", "_conflict")


def _did_you_mean(value: str, choices) -> str:
    close = get_close_matches(value, sorted(choices), n=1)
    return f" — did you mean '{close[0]}'?" if close else ""


def _validation_names(section: dict[str, Any], key: str) -> tuple[str, ...]:
    value = section.get(key)
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise RuntimeConfigError(f"validation.{key} must be a non-empty list of names")
    duplicate = next((v for i, v in enumerate(value) if v in value[:i]), None)
    if duplicate:
        raise RuntimeConfigError(f"validation.{key} lists '{duplicate}' twice")
    return tuple(value)


def _validation_name(section: dict[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise RuntimeConfigError(f"validation.{key} must be a name")
    return value


def _check_validation_labels(labels: list[str], key_titles: set[str]) -> None:
    seen: set[str] = set()
    for label in labels:
        folded = label.lower()
        if (
            not _LABEL.match(label)
            or folded in seen
            or folded in _CELLS_COLUMNS
            or folded in key_titles
            or folded.endswith(_CELLS_SUFFIXES)
        ):
            raise RuntimeConfigError(
                f"validation label '{label}' collides with a cells.csv column; "
                "rename the tier or the file"
            )
        seen.add(folded)


def _check_validation_models(
    cfg: dict[str, Any], models: tuple[str, ...], judge: str | None
) -> None:
    from psweep.extraction.llm_factory import model_identity

    aliases = cfg.get("models") or {}
    for key, names in (("models", models), ("judge", (judge,) if judge else ())):
        for alias in names:
            if alias not in aliases:
                raise RuntimeConfigError(
                    f"validation.{key}: '{alias}' is not a models: alias"
                    + _did_you_mean(alias, aliases)
                )
    extraction = cfg.get("extraction") or {}
    provider = extraction.get("provider")
    primary = model_identity(extraction.get("model"), models=aliases, provider=provider)
    seen: dict[tuple, str] = {}
    for alias in models:
        identity = model_identity(alias, models=aliases, provider=provider)
        if identity == primary:
            raise RuntimeConfigError(
                f"extraction.model is always a contender; remove '{alias}' from validation.models"
            )
        if identity in seen:
            raise RuntimeConfigError(
                f"validation.models '{seen[identity]}' and '{alias}' resolve to the same model"
            )
        seen[identity] = alias


def _validation_field_error(
    field: str, *, keys, quote_field, context, properties, passthrough
) -> str:
    from psweep.compilation.deduplicator import COL_ARTIFACT_ID, COL_RUN_ID
    from psweep.compilation.roster import METADATA_COLUMNS

    if field in keys:
        return f"validation.fields: '{field}' is a key field; keys are matched, not scored"
    if field == quote_field:
        return f"validation.fields: '{field}' is the quote_field; quotes are evidence, not scored"
    if field in context:
        return (
            f"validation.fields: '{field}' is a context field; "
            "add it to validation.key_fields to scope rows"
        )
    if field in properties:
        return f"validation.fields: '{field}' is excluded from the compiled table"
    if field in (*METADATA_COLUMNS, COL_RUN_ID, COL_ARTIFACT_ID):
        return f"validation.fields: '{field}' is a metadata column, not an extracted field"
    if field in passthrough():
        return f"validation.fields: '{field}' is a targets passthrough column, not an extracted field"
    return f"validation.fields: '{field}' is not a scorable field"


def _resolve_validation_section(cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Enforce the validation contract at load time; returns the normalized section."""
    from psweep.discovery.targets import TARGET_INPUT_FIELDS, iter_targets
    from psweep.extraction.roster import roster_fields
    from psweep.validation.align import context_fields, item_properties

    section = cfg["validation"]
    if not isinstance(section, dict) or not section:
        raise RuntimeConfigError("'validation' section must be a non-empty mapping")
    reject_unknown_keys(
        section, _ALLOWED_SECTION_FIELDS["validation"], "validation", config_path.as_posix()
    )
    models = _validation_names(section, "models")
    references = _validation_names(section, "references")
    fields = _validation_names(section, "fields")
    key_fields = _validation_names(section, "key_fields")
    judge = _validation_name(section, "judge")
    quote_field = _validation_name(section, "quote_field")
    min_accuracy = section.get("min_accuracy")
    if not models and not references:
        raise RuntimeConfigError("validation needs models or references to compare")
    if min_accuracy is not None:
        if (
            isinstance(min_accuracy, bool)
            or not isinstance(min_accuracy, (int, float))
            or not 0 < min_accuracy <= 1
        ):
            raise RuntimeConfigError("validation.min_accuracy must be in (0, 1]")
        if not references:
            raise RuntimeConfigError("min_accuracy needs validation.references")
    _check_validation_models(cfg, models, judge)
    paths = []
    for reference in references:
        path = Path(reference).expanduser()
        path = path if path.is_absolute() else config_path.parent / path
        if path.suffix.lower() != ".csv" or not path.is_file():
            raise RuntimeConfigError(
                f"validation.references: {reference} is not a CSV file ({path.as_posix()})"
            )
        paths.append(path.as_posix())

    resolved = resolve_command_config(command="compile", cli_values={}, config_data=cfg)
    metadata = resolve_compilation_metadata(resolved)
    if "csv" not in metadata.get_output_formats():
        raise RuntimeConfigError(
            "validation reads the compiled CSV; set compilation.output.default_format "
            "to csv or both"
        )
    titles = metadata.get_column_titles()
    excluded = set(metadata.get_output_exclude_fields())
    context = context_fields(metadata)
    roster = roster_fields(resolved, metadata.get_deduplication_key_fields())
    if roster:
        if key_fields:
            raise RuntimeConfigError(
                "roster mode keys rows by roster identity; remove validation.key_fields"
            )
        if quote_field:
            raise RuntimeConfigError("roster mode reads quotes from the evidence column")
        keys: tuple[str, ...] = roster
        properties = item_properties(metadata, None)
    else:
        if not key_fields:
            raise RuntimeConfigError(
                "rows mode needs validation.key_fields (identity.deduplication.key_fields "
                f"is a starting point: {', '.join(metadata.get_deduplication_key_fields())})"
            )
        output_fields = metadata.get_output_fields()
        for field in key_fields:
            if field not in output_fields:
                raise RuntimeConfigError(
                    f"validation.key_fields: '{field}' is not an output field"
                    + _did_you_mean(field, output_fields)
                )
            if titles.get(field, field) in excluded:
                raise RuntimeConfigError(
                    f"validation.key_fields: '{field}' is excluded from the compiled table"
                )
        keys = key_fields
        properties = item_properties(metadata, metadata.get_row_array())
        if quote_field:
            if quote_field not in properties:
                raise RuntimeConfigError(
                    f"validation.quote_field: '{quote_field}' is not an item field"
                    + _did_you_mean(quote_field, properties)
                )
            if "string" not in metadata.get_field_types().get(quote_field, set()):
                raise RuntimeConfigError(
                    f"validation.quote_field: '{quote_field}' must be a string field"
                )
            if quote_field in key_fields:
                raise RuntimeConfigError(
                    f"validation.quote_field: '{quote_field}' is also a key field"
                )
            if titles.get(quote_field, quote_field) in excluded:
                raise RuntimeConfigError(
                    f"validation.quote_field: '{quote_field}' is excluded from the compiled table"
                )
            if not set(key_fields) & context:
                raise RuntimeConfigError(
                    "quote_field matching needs a scope; add a context field to "
                    "validation.key_fields"
                )
    scorable = [
        f
        for f in properties
        if f not in keys and f != quote_field and titles.get(f, f) not in excluded
    ]

    def passthrough() -> set[str]:
        if not roster:
            return set()
        first = next(iter_targets(resolved["targets"], limit=1), {})
        return set(first) - set(roster) - TARGET_INPUT_FIELDS

    for field in fields:
        if field not in scorable:
            message = _validation_field_error(
                field,
                keys=keys,
                quote_field=quote_field,
                context=context,
                properties=properties,
                passthrough=passthrough,
            )
            if message.endswith("not a scorable field"):
                message += _did_you_mean(field, scorable)
            raise RuntimeConfigError(message)
    _check_validation_labels(
        [*models, *(Path(p).stem for p in paths)],
        {titles.get(f, f).lower() for f in keys},
    )
    return {
        "models": list(models),
        "references": paths,
        "judge": judge,
        "fields": list(fields),
        "key_fields": list(key_fields),
        "quote_field": quote_field,
        "min_accuracy": min_accuracy,
    }


def _read_config_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8")

    if suffix in {".yaml", ".yml"}:
        loaded = yaml.safe_load(raw_text)
    elif suffix == ".json":
        loaded = json.loads(raw_text)
    else:
        msg = (
            f"Unsupported config extension '{suffix}'. "
            "Use .yaml, .yml, or .json"
        )
        raise RuntimeConfigError(msg)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        msg = f"Config root must be an object: {path.as_posix()}"
        raise RuntimeConfigError(msg)

    return loaded


def _reject_implicit_section_overrides(config_path: Path) -> None:
    """Reject retired adjacent files instead of ignoring them."""
    if config_path.stem.lower() != "run":
        return
    adjacent = [
        path
        for section in _SECTION_NAMES
        for suffix in _CONFIG_SUFFIXES
        if (path := config_path.parent / f"{section}{suffix}").is_file()
    ]
    if not adjacent:
        return
    paths = ", ".join(path.name for path in adjacent)
    msg = (
        "Implicit section override files are retired: "
        f"{paths}. Move their settings into the corresponding sections of "
        f"{config_path.name} and remove the adjacent files, or load a "
        "section-wrapped config explicitly with --config."
    )
    raise RuntimeConfigError(msg)


def _model_references(config_data: dict[str, Any]) -> list[str]:
    """Collect every ``model:`` string under the discovery and extraction sections."""
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "model" and isinstance(value, str):
                    found.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for section_name in ("discovery", "extraction"):
        _walk(config_data.get(section_name))
    return found


def build_model_registry(
    config_data: dict[str, Any],
    llm_config: dict[str, Any] | None = None,
) -> ModelRegistry:
    """Build the registry and fail fast on invalid aliases or ``model:`` references."""
    try:
        registry = ModelRegistry.from_config(config_data, llm_config)
        registry.validate(references=_model_references(config_data))
    except ModelRegistryError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    return registry


_SCHEMA_REMEDY = "use top-level `schema:` or a schema.json beside the config"
_DERIVED_REMEDY = "stage outputs are derived from `domain` under outputs/<domain>/"
# Path keys the domain workspace derives; each maps to its user-facing remedy.
_DERIVED_PATH_KEYS: dict[tuple[str, str], str] = {
    ("extraction", "schema"): _SCHEMA_REMEDY,
    ("compilation", "schema"): _SCHEMA_REMEDY,
    ("extraction", "input_dir"): _DERIVED_REMEDY,
    ("extraction", "output_dir"): _DERIVED_REMEDY,
    ("compilation", "input_dir"): _DERIVED_REMEDY,
    ("compilation", "output_dir"): _DERIVED_REMEDY,
}


def _reject_derived_path_keys(
    config_data: dict[str, Any], config_path: Path
) -> None:
    found = [
        f"{section}.{key} ({remedy})"
        for (section, key), remedy in _DERIVED_PATH_KEYS.items()
        if isinstance(config_data.get(section), dict)
        and key in config_data[section]
    ]
    if found:
        msg = f"Remove from {config_path.as_posix()}: " + "; ".join(found)
        raise RuntimeConfigError(msg)


def _resolve_workspace_keys(
    config_data: dict[str, Any], config_path: Path
) -> None:
    """Default ``domain`` to the config directory name and type-check ``schema``."""
    domain = config_data.setdefault("domain", config_path.parent.name)
    try:
        Workspace(domain)
    except ValueError as exc:
        raise RuntimeConfigError(f"domain: {exc}") from exc
    schema = config_data.get("schema")
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise RuntimeConfigError("schema must be a non-empty path string")
    config_data["_config_path"] = config_path.as_posix()


def load_runtime_config_file(config_path: Path) -> dict[str, Any]:
    """Load a run-level or section-level runtime config file."""
    if not config_path.exists():
        msg = f"Config file not found: {config_path.as_posix()}"
        raise RuntimeConfigError(msg)

    _reject_implicit_section_overrides(config_path)
    config_data = _read_config_file(config_path)

    reject_unknown_keys(
        config_data, _ALLOWED_TOP_LEVEL, config_path=config_path.as_posix()
    )
    _reject_derived_path_keys(config_data, config_path)
    _resolve_workspace_keys(config_data, config_path)

    _validate_models_block(config_data.get("models"))
    _validate_model_context_windows_block(
        config_data.get("model_context_windows")
    )
    _validate_reasoning_models_block(config_data.get("reasoning_models"))

    discovery = config_data.get("discovery")
    if discovery is not None:
        resolve_discovery_section(discovery, config_path.as_posix())

    extraction = config_data.get("extraction")
    if extraction is not None:
        if not isinstance(extraction, dict):
            msg = "'extraction' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        _normalize_pages_block(extraction)
        _validate_extraction_section_schema(extraction)

    compilation = config_data.get("compilation")
    if compilation is not None:
        if not isinstance(compilation, dict):
            msg = "'compilation' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        _validate_compilation_section_schema(compilation)

    # Build the unified model registry (single normalization boundary) and
    # fail-fast validate tier definitions plus every model reference. Stored on
    # the returned config under a private key so callers can resolve tiers
    # without re-normalizing. Additive: sections that omit models still work.
    config_data["_model_registry"] = build_model_registry(config_data)

    if "validation" in config_data:
        config_data["validation"] = _resolve_validation_section(config_data, config_path)

    return config_data


def catalog_for_command(command: str) -> list[dict[str, str]]:
    """Return catalog entries for a command plus global variables."""
    section = _SECTION_ALIASES.get(command)
    if section is None:
        msg = f"Unsupported command for catalog: {command}"
        raise RuntimeConfigError(msg)
    return VARIABLE_CATALOG["global"] + VARIABLE_CATALOG[section]


_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "extraction": ("schema", "path"),
    "compilation": ("schema", "extracted_dir"),
    "validation": ("schema",),
}
# Required field -> (what the stage needs, YAML remedy, CLI remedy). Fields with
# no YAML remedy are always derived when a config file is present.
_MISSING_FIELD_HINTS: dict[str, tuple[str, str | None, str]] = {
    "schema": (
        "a schema",
        "add `schema:` to {config} or create {default_schema}",
        "pass --schema <file>",
    ),
    "path": ("input documents", None, "pass a documents path"),
    "extracted_dir": ("extraction records", None, "pass an extracted directory"),
}


def _missing_fields_message(
    section_name: str, missing: list[str], config_path: str | None
) -> str:
    parts = []
    for name in missing:
        need, yaml_remedy, cli_remedy = _MISSING_FIELD_HINTS[name]
        remedy = (
            yaml_remedy.format(
                config=config_path,
                default_schema=default_schema_path(Path(config_path)).as_posix(),
            )
            if config_path and yaml_remedy
            else cli_remedy
        )
        parts.append(f"{section_name} needs {need}: {remedy}")
    return "; ".join(parts)


# ── Top-level field map (all commands) ──────────────────────────

_FIELD_MAP: dict[str, str] = {
    "domain": "domain",
    "dry_run": "dry_run",
    "pages_csv": "pages_csv",
    "pages": "pages",
    "page_targeting": "page_targeting",
    "ocr_corrections": "ocr_corrections",
    "provider": "provider",
    "model": "model",
    "limit": "limit",
    "skip_existing": "skip_existing",
    "max_context": "max_context",
    "max_concurrent": "max_concurrent",
    "timeout_seconds": "timeout_seconds",
    "report_format": "report_format",
    "fail_on_suspicious": "fail_on_suspicious",
    "provenance_policy": "provenance_policy",
    "normalization": "normalization",
    "compilation_output": "output",
    "severity_tokens": "severity_tokens",
}


def config_key_paths(command: str) -> dict[str, str]:
    """Map resolved field names to the config key paths users write."""
    section_name = _SECTION_ALIASES[command]
    if section_name == "discovery":
        return discovery_key_paths()
    return {
        field: f"{section_name}.{key}" for field, key in _FIELD_MAP.items()
    }


def _merge_config_fields(
    *,
    section_name: str,
    section: dict[str, Any],
    cli_values: dict[str, Any],
    config_path: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    if section_name == "discovery":
        for field, (value, source) in resolve_discovery_section(
            section, config_path
        ).items():
            merged[field], sources[field] = value, source
    else:
        for cli_key, section_key in _FIELD_MAP.items():
            if section_key in section:
                merged[cli_key] = section.get(section_key)
                sources[cli_key] = f"config.{section_name}.{section_key}"

    for cli_key, value in cli_values.items():
        if value is not None:
            merged[cli_key] = value
            sources[cli_key] = "cli"

    return merged, sources


def resolve_compilation_metadata(resolved: dict[str, Any]):
    """Validate the merged schema/runtime contract before any compilation work."""
    from psweep.exceptions import SchemaError, ConfigurationError
    from psweep.utils.schema_metadata import SchemaMetadata

    try:
        return SchemaMetadata.for_compilation(Path(resolved["schema"]), resolved)
    except (SchemaError, ConfigurationError, ValueError, OSError) as exc:
        raise RuntimeConfigError(f"Compilation contract is invalid: {exc}") from exc


# Section -> (resolved field, Workspace attribute) pairs for derived stage paths.
_WORKSPACE_DEFAULTS: dict[str, tuple[tuple[str, str], ...]] = {
    "extraction": (("path", "curated"), ("output", "extracted")),
    "compilation": (("extracted_dir", "extracted"), ("output", "compiled")),
    "validation": (),
}
# Config-less runs infer the domain from this input field.
_DOMAIN_PROBE_FIELD = {"extraction": "path", "compilation": "extracted_dir"}


def _apply_workspace_defaults(
    section_name: str,
    merged: dict[str, Any],
    sources: dict[str, str],
    cfg: dict[str, Any],
) -> None:
    """Fill the schema and stage paths that the domain workspace derives."""
    config_path = cfg.get("_config_path")
    if not merged.get("schema"):
        if cfg.get("schema"):
            merged["schema"], sources["schema"] = cfg["schema"], "config.schema"
        elif config_path and default_schema_path(Path(config_path)).is_file():
            merged["schema"] = default_schema_path(Path(config_path)).as_posix()
            sources["schema"] = "derived"
    if not merged.get("domain"):
        probe = merged.get(_DOMAIN_PROBE_FIELD.get(section_name, ""))
        if not probe:
            return
        merged["domain"], sources["domain"] = infer_domain(Path(probe)), "derived"
    try:
        workspace = Workspace(merged["domain"])
    except ValueError as exc:
        raise RuntimeConfigError(f"domain: {exc}") from exc
    for field, attr in _WORKSPACE_DEFAULTS[section_name]:
        if not merged.get(field):
            merged[field] = getattr(workspace, attr).as_posix()
            sources[field] = "derived"


def resolve_command_config(
    *,
    command: str,
    cli_values: dict[str, Any],
    config_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve command inputs from runtime config and CLI overrides."""
    section_name = _SECTION_ALIASES.get(command)
    if section_name is None:
        msg = f"Unsupported command config resolution target: {command}"
        raise RuntimeConfigError(msg)

    cfg = config_data or {}
    section = cfg.get(section_name) or {}
    if section and not isinstance(section, dict):
        msg = f"'{section_name}' section must be an object in runtime config"
        raise RuntimeConfigError(msg)

    config_path = cfg.get("_config_path")
    if section_name in _ALLOWED_SECTION_FIELDS:
        reject_unknown_keys(
            section, _ALLOWED_SECTION_FIELDS[section_name], section_name, config_path
        )
    if section_name == "extraction":
        _validate_extraction_section_schema(section)
    merged, sources = _merge_config_fields(
        section_name=section_name,
        section=section,
        cli_values=cli_values,
        config_path=config_path,
    )

    if cli_values.get("domain") is None and "domain" in cfg:
        merged["domain"] = cfg["domain"]
        sources["domain"] = "config.domain"
    if section_name != "discovery":
        _apply_workspace_defaults(section_name, merged, sources, cfg)
    if section_name == "validation":
        merged["validation"], sources["validation"] = dict(section), "config.validation"
    if section_name in ("extraction", "compilation", "validation") and isinstance(
        cfg.get("discovery"), dict
    ):
        discovery = resolve_discovery_section(cfg["discovery"], config_path)
        for field in ("targets", "partition_by"):
            if field in discovery:
                merged[field], sources[field] = discovery[field]

    missing = [
        name for name in _REQUIRED_FIELDS.get(section_name, ()) if not merged.get(name)
    ]
    if missing:
        raise RuntimeConfigError(
            _missing_fields_message(section_name, missing, cfg.get("_config_path"))
        )

    # Top-level model alias map is available to every command (process,
    # discover, compile) so any LLM stage can resolve an alias reference.
    if "models" in cfg:
        merged["models"] = cfg.get("models")
        sources["models"] = "config.models"

    # Top-level per-model context-window map feeds the extraction fail-fast
    # guard; passed through untouched (no model names hardcoded in code).
    if "model_context_windows" in cfg:
        merged["model_context_windows"] = cfg.get("model_context_windows")
        sources["model_context_windows"] = "config.model_context_windows"

    # Extra reasoning-model markers (unioned with built-in defaults in the LLM
    # client); passed through untouched for the extraction stage to consume.
    if "reasoning_models" in cfg:
        merged["reasoning_models"] = cfg.get("reasoning_models")
        sources["reasoning_models"] = "config.reasoning_models"

    # The unified model registry (built + validated at load time) is threaded
    # through so every LLM stage resolves tiers/credentials from one instance.
    if "_model_registry" in cfg:
        merged["_model_registry"] = cfg.get("_model_registry")
        sources["_model_registry"] = "config._model_registry"

    merged["_config_sources"] = sources
    return merged
