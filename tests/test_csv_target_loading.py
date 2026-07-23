"""Test CSV target loading functionality in runtime config loader."""

import pytest
from pathlib import Path
from psweep.config import load_runtime_config_file, RuntimeConfigError


class TestCsvTargetLoading:
    """Test loading targets from CSV files."""

    def test_load_targets_from_csv_basic(self, tmp_path: Path):
        """Load targets from a basic CSV with utility and state columns."""
        # Create CSV file
        csv_path = tmp_path / "targets.csv"
        csv_path.write_text(
            "utility,state\nPG&E,CA\nDuke Energy,NC\n",
            encoding="utf-8"
        )

        # Create config file that references the CSV
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        
        # Verify targets were loaded from CSV
        assert "targets" in config_data["discovery"]
        targets = config_data["discovery"]["targets"]
        assert len(targets) == 2
        assert targets[0]["utility"] == "PG&E"
        assert targets[0]["state"] == "CA"
        assert targets[1]["utility"] == "Duke Energy"
        assert targets[1]["state"] == "NC"
        
        # CSV reference should be removed after processing
        assert "targets_csv" not in config_data["discovery"]

    def test_load_targets_from_csv_with_multiple_columns(self, tmp_path: Path):
        """Load targets from CSV with many columns (for template substitution)."""
        csv_path = tmp_path / "targets.csv"
        csv_path.write_text(
            "manufacturer,power_class_kw\nGenerac,200-300\nJohn Deere,50-100\n",
            encoding="utf-8"
        )

        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        targets = config_data["discovery"]["targets"]
        
        assert len(targets) == 2
        assert targets[0]["manufacturer"] == "Generac"
        assert targets[0]["power_class_kw"] == "200-300"

    def test_load_targets_from_csv_with_inline_targets(self, tmp_path: Path):
        """CSV targets should be merged with inline targets (CSV first)."""
        csv_path = tmp_path / "targets.csv"
        csv_path.write_text(
            "utility,state\nPG&E,CA\n",
            encoding="utf-8"
        )

        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  targets:
    - utility: Duke Energy
      state: NC
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        targets = config_data["discovery"]["targets"]
        
        # CSV targets should come first, then inline targets
        assert len(targets) == 2
        assert targets[0]["utility"] == "PG&E"
        assert targets[1]["utility"] == "Duke Energy"

    def test_load_targets_from_csv_relative_path(self, tmp_path: Path):
        """CSV path can be relative to the config file directory."""
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        
        csv_path = config_dir / "targets.csv"
        csv_path.write_text(
            "name,value\nTest1,Value1\n",
            encoding="utf-8"
        )

        run_path = config_dir / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        targets = config_data["discovery"]["targets"]
        
        assert len(targets) == 1
        assert targets[0]["name"] == "Test1"

    def test_load_targets_from_csv_missing_file(self, tmp_path: Path):
        """Error when CSV file doesn't exist."""
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: nonexistent.csv
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeConfigError) as exc_info:
            load_runtime_config_file(run_path)
        
        assert "Targets CSV file not found" in str(exc_info.value)

    def test_load_targets_from_csv_empty_fields(self, tmp_path: Path):
        """Empty CSV fields are converted to None."""
        csv_path = tmp_path / "targets.csv"
        csv_path.write_text(
            "utility,state,sector\nPG&E,CA,residential\nDuke Energy,NC,\n",
            encoding="utf-8"
        )

        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  seeker:
    provider: serpapi
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        targets = config_data["discovery"]["targets"]
        
        assert targets[0]["sector"] == "residential"
        assert targets[1]["sector"] is None  # Empty string becomes None

    def test_csv_targets_can_be_used_with_query_families(self, tmp_path: Path):
        """CSV target fields work with query_families template substitution."""
        csv_path = tmp_path / "targets.csv"
        csv_path.write_text(
            "utility,state\nPG&E,CA\n",
            encoding="utf-8"
        )

        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  targets_csv: targets.csv
  query_families:
    utility_tariff:
      - "{utility} tariff {state} pdf"
  seeker:
    provider: serpapi
    use_query_family: utility_tariff
extraction:
  input_dir: documents
  schema: schemas/test.json
""",
            encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        
        # Verify targets loaded with proper structure for template substitution
        assert "targets" in config_data["discovery"]
        assert "query_families" in config_data["discovery"]
        targets = config_data["discovery"]["targets"]
        assert targets[0]["utility"] == "PG&E"
        assert targets[0]["state"] == "CA"
