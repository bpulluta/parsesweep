from streamline_extract.utils.config import Config


def test_setup_directories_uses_data_root(tmp_path):
    cfg = Config(project_root=tmp_path)
    cfg.setup_directories()

    assert cfg.data_root.exists()
    assert cfg.permits_dir.exists()
    assert cfg.extracted_dir.exists()
    assert cfg.outputs_dir.exists()


def test_repr_does_not_reference_removed_attributes(tmp_path):
    cfg = Config(project_root=tmp_path)
    rendered = repr(cfg)

    assert "data_root=" in rendered
    assert "provider=" in rendered
    assert "api_key=" in rendered
