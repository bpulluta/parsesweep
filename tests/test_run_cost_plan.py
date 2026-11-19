"""``psweep run`` shows paid calls in its plan and warns after a partial discovery."""

import json
import sys
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from psweep.cli.app import _paid_call_rows, partial_discovery_warning
from psweep.config.model_registry import ModelRegistry
from psweep.cli.main import cli
from psweep.pipeline import execute_run_stages
from psweep.workspace import Workspace

FULL_DISCOVERY = {
    "targets": "targets.csv",
    "queries": ['"{name}" manual', "{name} guide"],
    "search": {"fallback_queries": ["{name} spec"]},
    "selection": {"max_per_target": 4},
    "document_review": {
        "document_description": "A manual for {name}.",
        "model": "cheap",
        "keep_top": 2,
        "escalate_model": "strong",
    },
    "runtime": {"max_review_calls": 12, "max_search_calls": 50},
}


def _plan(tmp_path, monkeypatch, schema_path, config, *args):
    """Run ``psweep run`` answering "n" to the prompt; assert no stage executed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "targets.csv").write_text("name\nA\nB\nC\n", encoding="utf-8")
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"domain": "demo", "schema": str(schema_path), **config}))
    with patch("psweep.pipeline.subprocess.run") as stage:
        result = CliRunner().invoke(cli, ["run", "--config", str(path), *args], input="n\n")
    stage.assert_not_called()
    assert result.exit_code == 0, result.output
    assert "Cancelled." in result.output
    return " ".join(result.output.split())  # undo table wrapping


def test_plan_lists_paid_calls_and_unpriced_models(tmp_path, monkeypatch, schema_path):
    out = _plan(
        tmp_path,
        monkeypatch,
        schema_path,
        {
            "models": {"cheap": "gpt-4o-mini", "strong": "acme-unpriced-1", "loc": "gpt-4o"},
            "discovery": FULL_DISCOVERY,
            "extraction": {
                "model": "strong",
                "pages": {"auto_locate": {"section_description": "x", "model": "loc"}},
            },
        },
    )
    assert "SerpApi ≤6 queries for 3 targets (cache on; ceiling 50) + fallback" in out
    assert "gpt-4o-mini grades ≤4 downloads/target, keeps 2" in out
    assert "escalate acme-unpriced-1" in out
    assert "ceiling 12/target" in out
    assert "acme-unpriced-1; page targeting gpt-4o" in out
    assert "Price unknown" in out
    assert "(spend will not be metered in dollars)" in out
    assert out.count("acme-unpriced-1 (spend") == 1  # de-duplicated


def test_plan_omits_paid_rows_for_stages_that_are_off(tmp_path, monkeypatch, schema_path):
    out = _plan(
        tmp_path,
        monkeypatch,
        schema_path,
        {"models": {"m": "gpt-4o-mini"}, "extraction": {"model": "m"}},
        "--skip-discover",
    )
    for row in ("Search", "Review", "Reconcile", "Price unknown"):
        assert row not in out
    assert "Extract" in out


def test_plan_prices_a_target_query_field_once_per_target(tmp_path, monkeypatch, schema_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "targets.csv").write_text("name,query\nA,alpha\nB,beta\n", encoding="utf-8")
    path = tmp_path / "run.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "domain": "demo",
                "schema": str(schema_path),
                "discovery": {"targets": "targets.csv", "queries": ["a {name}", "b {name}"]},
            }
        )
    )
    with patch("psweep.pipeline.subprocess.run") as stage:
        result = CliRunner().invoke(cli, ["run", "--config", str(path)], input="n\n")
    stage.assert_not_called()
    assert "SerpApi ≤2 queries for 2 targets (cache on; ceiling none)" in " ".join(
        result.output.split()
    )


def test_execute_run_stages_reports_each_partial_stage_once():
    ok = [sys.executable, "-c", "pass"]
    partial = [sys.executable, "-c", "import sys; sys.exit(3)"]
    seen: list[str] = []
    order: list[str] = []
    stages = [("discover", partial), ("extract", ok), ("compile", ok)]
    result = execute_run_stages(
        stages, on_stage=lambda _i, stage: order.append(stage), on_partial=seen.append
    )
    assert result == ["discover"]
    assert seen == ["discover"]
    assert order == ["discover", "extract", "compile"]


def test_partial_warning_counts_incomplete_checkpoint_targets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checkpoint = Workspace("demo").root / "discovered" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "entries": {
                    "a": {"incomplete": True},
                    "b": {"incomplete": True},
                    "c": {"run_id": "r1"},
                }
            }
        ),
        encoding="utf-8",
    )
    message = partial_discovery_warning(Workspace("demo"))
    assert "Discovery ended partial" in message
    assert "2 targets in the checkpoint" in message
    assert "psweep discover" in message


def test_partial_warning_without_a_checkpoint_has_no_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    message = partial_discovery_warning(Workspace("demo"))
    assert message.startswith("Discovery ended partial.")
    assert "targets in the checkpoint" not in message


def test_run_warns_before_extraction_only_after_partial_discovery(
    tmp_path, monkeypatch, schema_path
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "targets.csv").write_text("name\nA\n", encoding="utf-8")
    path = tmp_path / "run.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "domain": "demo",
                "schema": str(schema_path),
                "discovery": {"targets": "targets.csv", "queries": ["{name}"]},
            }
        )
    )

    def fake(stages, on_stage=None, on_partial=None):
        for stage, _ in stages:
            if stage == "discover" and on_partial is not None:
                on_partial(stage)
        return ["discover"]

    with patch("psweep.cli.app.execute_run_stages", fake):
        result = CliRunner().invoke(cli, ["run", "--config", str(path), "--yes"])
    assert "Discovery ended partial" in result.output

    with patch("psweep.cli.app.execute_run_stages", fake):
        skipped = CliRunner().invoke(cli, ["run", "--config", str(path), "--yes", "--skip-extract"])
    assert "Discovery ended partial" not in skipped.output


def test_reconcile_and_validation_models_are_priced_without_a_run():
    registry = ModelRegistry.from_config({"models": {"a": "gpt-4o", "odd": "acme-unpriced-2"}})
    cfg = {
        "_model_registry": registry,
        "extraction": {"model": "a"},
        "compilation": {"reconciliation": {"enabled": True, "model": "odd"}},
        "validation": {"models": ["a"], "judge": "odd"},
    }
    rows = _paid_call_rows(cfg, None, ["extract", "compile", "validate"])
    assert rows["Reconcile"] == "acme-unpriced-2"
    assert rows["Price unknown"] == "acme-unpriced-2 (spend will not be metered in dollars)"
    assert _paid_call_rows({**cfg, "compilation": {}}, None, ["compile"]) == {}
