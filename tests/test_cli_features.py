"""Tests for model pricing, cost estimates, and CLI UI helpers."""

import pytest
from psweep.cli.ui import key_values
from psweep.utils.model_pricing import estimate_extraction_cost, get_model_pricing


class TestModelPricing:
    """Pricing resolves through LiteLLM's bundled cost map."""

    def test_exact_name(self):
        assert get_model_pricing("gpt-4.1-mini") == (0.4, 1.6)

    @pytest.mark.parametrize(
        "name", ["azure/gpt-4.1-mini", "openai/gpt-4.1-mini", "AZURE/GPT-4.1-MINI"]
    )
    def test_provider_prefixed(self, name):
        assert get_model_pricing(name) == get_model_pricing("gpt-4.1-mini")

    def test_provider_prefix_added_when_only_prefixed_key_exists(self):
        assert get_model_pricing("anthropic/claude-haiku-4-5") == (1.0, 5.0)

    def test_unknown_returns_none(self):
        assert get_model_pricing("totally-unknown-model-xyz") is None

    def test_estimate_known_model(self):
        est = estimate_extraction_cost(4_000_000, "gpt-4.1-mini")
        assert est.total_cost == pytest.approx(0.4 + 0.1 * 1.6)
        assert est.format_cost() == "$0.56"

    def test_estimate_unknown_model_is_explicit(self):
        est = estimate_extraction_cost(4000, "mystery-model")
        assert est.total_cost is None
        assert est.input_rate is None and est.output_rate is None
        assert est.format_cost() == "price unknown for mystery-model"

    def test_estimate_command_reports_unknown_price(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from psweep.cli import utils_commands as uc

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.txt").write_text("x" * 4000)

        class _Config:
            llm_config = {"model": "mystery-model"}

        monkeypatch.setattr(uc, "get_config", lambda: _Config())
        monkeypatch.setattr(uc, "extract_text_from_document", lambda *a, **k: "x" * 4000)
        result = CliRunner().invoke(uc.estimate, [str(docs)], input="n\n")
        assert result.exit_code == 0, result.output
        assert "price unknown for mystery-model" in result.output
        assert "$" not in result.output


class TestUIFunctions:
    """Test the unified key/value renderer."""

    def test_key_values_from_dict(self):
        """A mapping renders as a titled table (no panel by default)."""
        config = {
            "Documents": "10 files",
            "Schema": "test_schema.json",
            "Output": "extracted/test/",
        }
        table = key_values(config, title="Test Config")
        assert table is not None
        # Cohesive style: plain title, no decorative emoji prefix.
        assert table.title == "Test Config"
        assert table.row_count == 3

    def test_key_values_from_rows(self):
        """A sequence of (key, value) pairs renders the same way."""
        rows = [("Processed", "10"), ("Successful", "9"), ("Failed", "1")]
        table = key_values(rows)
        assert table is not None
        assert table.row_count == 3

    def test_key_values_as_panel(self):
        """as_panel wraps the table in a bordered panel with the title."""
        from rich.panel import Panel

        panel = key_values({"Total Cost": "$0.50"}, title="Summary", as_panel=True)
        assert isinstance(panel, Panel)
        assert panel.title == "Summary"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
