"""Tests for the CLI cleanup audit (shared estimator, counts).

Covers:
- the single cost/token estimator in ``psweep.utils.model_pricing``
- the shared on-disk artifact counters in ``psweep.cli.app``
- the ``preview`` command pricing bugfix (configured model, not DEFAULT_MODEL)
"""


import click.testing
import pytest

from psweep.utils.model_pricing import (
    CHARS_PER_TOKEN,
    OUTPUT_TOKEN_RATIO,
    TOKENS_PER_SECOND,
    estimate_extraction_cost,
)
from psweep.pipeline import (
    count_curated_documents as _count_curated_documents,
    count_extracted_documents as _count_extracted_documents,
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
    assert est.input_rate == 0.15
    assert est.output_rate == 0.60
    assert est.total_cost == pytest.approx((1000 * 0.15 + 100 * 0.60) / 1_000_000)
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

    # 4,000,000 chars -> 1,000,000 tokens: gpt-4o ($2.50 in + $1.00 out) = $3.50.
    monkeypatch.setattr(
        uc, "extract_text_from_document", lambda *a, **k: "x" * 4_000_000
    )

    class _FakeConfig:
        llm_config = {"model": "gpt-4o"}
        config_dir = tmp_path / "no_config"  # empty -> no schema detection
        project_root = tmp_path

    monkeypatch.setattr(uc, "get_config", lambda: _FakeConfig())

    result = click.testing.CliRunner().invoke(uc.preview, [str(doc)])
    assert result.exit_code == 0, result.output
    # Configured model shown (gpt-4o), NOT the default gpt-4o-mini.
    assert "gpt-4o" in result.output
    assert "gpt-4o-mini" not in result.output
    assert "$3.50" in result.output


# ── shared on-disk artifact counters ─────────────────────────────────────────


def test_count_curated_documents_skips_sidecars(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    (d / "a.pdf").write_text("x")
    (d / "b.docx").write_text("x")
    (d / "c.json").write_text("[]")  # JSON data export -> a document
    (d / "a.text").write_text("x")  # .text sidecar -> excluded
    (d / ".review").mkdir()
    (d / ".review" / "a.pdf.json").write_text("{}")  # hidden -> excluded
    assert _count_curated_documents(d) == 3


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
