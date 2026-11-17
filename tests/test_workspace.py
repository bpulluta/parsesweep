"""Domain workspace layout: derived stage paths, schema defaults, removed keys."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.workspace import (
    Workspace,
    default_schema_path,
    domain_schemas,
    infer_domain,
)


def _domain_config(tmp_path: Path, body: str, domain: str = "demo") -> Path:
    config_dir = tmp_path / "config" / domain
    config_dir.mkdir(parents=True)
    path = config_dir / "run.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _resolve(path: Path, command: str) -> dict:
    return resolve_command_config(
        command=command,
        cli_values={},
        config_data=load_runtime_config_file(path),
    )


def test_workspace_derives_every_stage_dir_from_domain():
    ws = Workspace("demo")
    assert ws.root == Path("outputs/demo")
    assert ws.discovered == Path("outputs/demo/discovered")
    assert ws.curated == Path("outputs/demo/discovered/curated")
    assert ws.latest == Path("outputs/demo/discovered/latest")
    assert ws.extracted == Path("outputs/demo/extracted")
    assert ws.validated == Path("outputs/demo/validated")
    assert ws.compiled == Path("outputs/demo/compiled")
    assert ws.reconciliation_cache == Path("outputs/demo/.reconciliation")
    assert not ws.reconciliation_cache.is_relative_to(ws.compiled)


@pytest.mark.parametrize("domain", ["", ".", "..", "a/b", "a\\b", "/abs"])
def test_workspace_rejects_non_directory_names(domain):
    with pytest.raises(ValueError, match="single directory name"):
        Workspace(domain)


def test_default_schema_and_domain_schema_discovery(tmp_path):
    assert default_schema_path(tmp_path / "x" / "run.yaml") == tmp_path / "x" / "schema.json"
    for name in ("b", "a"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "schema.json").write_text("{}")
    (tmp_path / "no_schema").mkdir()
    assert domain_schemas(tmp_path) == [tmp_path / "a/schema.json", tmp_path / "b/schema.json"]


def test_infer_domain_prefers_outputs_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extracted = Workspace("solar").extracted
    extracted.mkdir(parents=True)
    assert infer_domain(extracted) == "solar"
    docs = tmp_path / "documents" / "tariffs"
    docs.mkdir(parents=True)
    (docs / "a.pdf").write_text("x")
    assert infer_domain(docs) == "tariffs"
    assert infer_domain(docs / "a.pdf") == "tariffs"


def test_domain_defaults_to_config_dir_and_outputs_are_derived(tmp_path):
    path = _domain_config(tmp_path, "extraction:\n  max_context: 1000\n")
    (path.parent / "schema.json").write_text("{}")
    extraction = _resolve(path, "extract")
    assert extraction["domain"] == "demo"
    assert extraction["schema"] == (path.parent / "schema.json").as_posix()
    assert extraction["path"] == "outputs/demo/discovered/curated"
    assert extraction["output"] == "outputs/demo/extracted"
    sources = extraction["_config_sources"]
    assert {sources[k] for k in ("schema", "path", "output")} == {"derived"}
    compilation = _resolve(path, "compile")
    assert compilation["extracted_dir"] == "outputs/demo/extracted"
    assert compilation["output"] == "outputs/demo/compiled"


def test_top_level_schema_overrides_sibling_default(tmp_path):
    path = _domain_config(
        tmp_path,
        "schema: shared/contract.json\nextraction:\n  skip_existing: true\n",
    )
    (path.parent / "schema.json").write_text("{}")
    for command in ("extract", "compile"):
        resolved = _resolve(path, command)
        assert resolved["schema"] == "shared/contract.json"
        assert resolved["_config_sources"]["schema"] == "config.schema"
    assert _resolve(path, "extract")["path"] == "outputs/demo/discovered/curated"


@pytest.mark.parametrize(
    "section,key",
    [
        ("extraction", "schema"),
        ("extraction", "input_dir"),
        ("extraction", "output_dir"),
        ("compilation", "schema"),
        ("compilation", "input_dir"),
        ("compilation", "output_dir"),
    ],
)
def test_derived_path_keys_are_rejected(tmp_path, section, key):
    path = _domain_config(tmp_path, f"{section}:\n  {key}: somewhere\n")
    with pytest.raises(RuntimeConfigError, match=rf"Remove from .*{section}\.{key}"):
        load_runtime_config_file(path)


@pytest.mark.parametrize("command", ["extract", "compile"])
def test_missing_schema_error_names_yaml_key_and_default_file(tmp_path, command):
    path = _domain_config(tmp_path, "domain: demo\n")
    with pytest.raises(RuntimeConfigError) as exc:
        _resolve(path, command)
    message = str(exc.value)
    assert "needs a schema" in message
    assert f"add `schema:` to {path.as_posix()}" in message
    assert f"create {(path.parent / 'schema.json').as_posix()}" in message
    assert "Missing required" not in message


def test_missing_schema_without_config_points_to_cli_flag():
    with pytest.raises(RuntimeConfigError, match="pass --schema"):
        resolve_command_config(
            command="extract", cli_values={"path": "documents/demo"}, config_data=None
        )


def test_run_validate_config_reports_missing_schema_in_yaml_terms(tmp_path):
    path = _domain_config(tmp_path, "domain: demo\n")
    result = CliRunner().invoke(cli, ["run", "--config", str(path), "--validate-config"])
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "extraction needs a schema: add `schema:` to" in flat
