"""Tests for the CLI cleanup audit (shared estimator, path resolver, counts).

Covers:
- the single cost/token estimator in ``psweep.cli.cost_tracker``
- the shared path-layout resolvers in ``psweep.pipeline``
- the shared on-disk artifact counters in ``psweep.cli.app``
- the ``preview`` command pricing bugfix (configured model, not DEFAULT_MODEL)
"""

from pathlib import Path

import click.testing
import pytest

from psweep.cli.cost_tracker import (
    CHARS_PER_TOKEN,
    OUTPUT_TOKEN_RATIO,
    TOKENS_PER_SECOND,
    estimate_extraction_cost,
)
from psweep.pipeline import (
    discover_domain,
    resolve_compile_output_dir,
    resolve_extract_output_dir,
    swap_layout_component,
)
from psweep.cli.app import (
    _count_curated_documents,
    _count_extracted_documents,
)


# ── shared cost/token estimator ──────────────────────────────────────────────


def test_estimate_extraction_cost_locks_math():
    # 4000 chars -> 1000 input tokens, output = 10% of input.
    est = estimate_extraction_cost(4000, "gpt-4o-mini")
    assert CHARS_PER_TOKEN == 4
    assert OUTPUT_TOKEN_RATIO == 0.1
    assert TOKENS_PER_SECOND == 100

    assert est.model == "gpt-4o-mini"
    assert est.input_tokens == 1000
    assert est.output_tokens == pytest.approx(100.0)
    # gpt-4o-mini pricing: (0.15, 0.60) per 1M tokens.
    assert est.input_rate == 0.15
    assert est.output_rate == 0.60
    assert est.input_cost == pytest.approx(1000 / 1_000_000 * 0.15)
    assert est.output_cost == pytest.approx(100 / 1_000_000 * 0.60)
    assert est.total_cost == pytest.approx(est.input_cost + est.output_cost)
    # 1000 tokens / (100 tokens/sec * 1 worker) = 10 seconds.
    assert est.estimated_seconds == pytest.approx(10.0)
    assert est.estimated_minutes == pytest.approx(10.0 / 60)


def test_estimate_extraction_cost_scales_time_by_workers():
    est = estimate_extraction_cost(4000, "gpt-4o-mini", workers=4)
    # Cost is worker-independent; only time shrinks.
    assert est.input_tokens == 1000
    assert est.estimated_seconds == pytest.approx(1000 / (100 * 4))


def test_estimate_extraction_cost_floors_tokens_from_float_chars():
    # estimate/extract pass a float (avg_chars * count); tokens floor toward 0.
    est = estimate_extraction_cost(4003.9, "gpt-4o-mini")
    assert est.input_tokens == 1000


def test_preview_uses_configured_model_price(tmp_path, monkeypatch):
    """The ``preview`` command must price with the configured model, not the
    hardcoded DEFAULT_MODEL (gpt-4o-mini)."""
    from psweep.cli import utils_commands as uc

    doc = tmp_path / "sample.txt"
    doc.write_text("hello world")

    # 4,000,000 chars -> 1,000,000 tokens so gpt-4o costs a clearly non-zero,
    # model-distinguishable amount ($5.00 in + $1.50 out = $6.50).
    monkeypatch.setattr(
        uc, "extract_text_from_document", lambda *a, **k: "x" * 4_000_000
    )

    class _FakeConfig:
        llm_config = {"model": "gpt-4o"}
        schema_dir = tmp_path / "no_schemas"  # empty -> no schema detection

    monkeypatch.setattr(uc, "get_config", lambda: _FakeConfig())

    result = click.testing.CliRunner().invoke(uc.preview, [str(doc)])
    assert result.exit_code == 0, result.output
    # Configured model shown (gpt-4o), NOT the default gpt-4o-mini.
    assert "gpt-4o" in result.output
    assert "gpt-4o-mini" not in result.output
    # gpt-4o pricing (5.00 / 15.00) -> $6.50, not gpt-4o-mini's $0.21.
    assert "$6.50" in result.output


# ── shared path-layout resolver ──────────────────────────────────────────────


def test_swap_layout_component_swaps_first_component():
    assert swap_layout_component(
        Path("extracted/tariffs"), "extracted", "compiled"
    ) == Path("compiled/tariffs")


def test_swap_layout_component_fallback_to_cwd():
    assert swap_layout_component(
        Path("foo/bar"), "extracted", "compiled"
    ) == Path.cwd() / "compiled" / "bar"


def test_resolve_compile_output_dir_swaps_extracted():
    assert resolve_compile_output_dir(
        Path("extracted/tariffs")
    ) == Path("compiled/tariffs")


def test_resolve_extract_output_dir_swaps_documents_dir():
    assert resolve_extract_output_dir(
        Path("documents/tariffs"), is_dir=True
    ) == Path("extracted/tariffs")


def test_resolve_extract_output_dir_uses_parent_for_files():
    assert resolve_extract_output_dir(
        Path("documents/tariffs/a.pdf"), is_dir=False
    ) == Path("extracted/tariffs")


def test_resolve_extract_output_dir_discovered_domain(tmp_path):
    curated = tmp_path / "discovered" / "mydomain" / "curated"
    curated.mkdir(parents=True)
    assert discover_domain(curated) == "mydomain"
    assert resolve_extract_output_dir(curated, is_dir=True) == (
        Path.cwd() / "extracted" / "mydomain"
    )


# ── shared on-disk artifact counters ─────────────────────────────────────────


def test_count_curated_documents_skips_sidecars_and_json(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    (d / "a.pdf").write_text("x")
    (d / "b.docx").write_text("x")
    (d / "a.text").write_text("x")  # .text sidecar -> excluded
    (d / "meta.json").write_text("{}")  # .json -> excluded
    assert _count_curated_documents(d) == 2


def test_count_curated_documents_missing_dir(tmp_path):
    assert _count_curated_documents(tmp_path / "nope") == 0


def test_count_extracted_documents_excludes_manifests(tmp_path):
    d = tmp_path / "extracted"
    d.mkdir()
    (d / "a.json").write_text("{}")
    (d / "b.json").write_text("{}")
    manifests = d / "run_manifests"
    manifests.mkdir()
    (manifests / "m.json").write_text("{}")
    assert _count_extracted_documents(d) == 2


def test_count_extracted_documents_missing_dir(tmp_path):
    assert _count_extracted_documents(tmp_path / "nope") == 0
