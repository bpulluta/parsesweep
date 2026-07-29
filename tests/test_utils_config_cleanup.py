from psweep.utils.config import Config


def test_config_exposes_core_paths(tmp_path):
    cfg = Config(project_root=tmp_path)

    assert cfg.project_root == tmp_path
    assert cfg.schema_dir == tmp_path / "schemas"
    assert cfg.default_schema == (
        tmp_path / "schemas" / "example_utility_rate_schema.json"
    )
    assert isinstance(cfg.llm_config, dict)


def test_repr_reports_core_fields(tmp_path):
    cfg = Config(project_root=tmp_path)
    rendered = repr(cfg)

    assert "project_root=" in rendered
    assert "provider=" in rendered
    assert "api_key=" in rendered
