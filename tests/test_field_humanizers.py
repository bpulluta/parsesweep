"""Tests for the shared field-name humanization helpers.

These lock the single-source label helpers in ``utils.normalizers`` that
back column-name generation across compilation and the CLI.
"""

from psweep.utils.normalizers import humanize_field_name, camel_to_title


class TestHumanizeFieldName:
    def test_snake_case(self):
        assert humanize_field_name("charge_type") == "Charge Type"

    def test_camel_case(self):
        assert humanize_field_name("annualConsumption") == "Annual Consumption"

    def test_mixed(self):
        assert humanize_field_name("utility_rateName") == "Utility Rate Name"


class TestCamelToTitle:
    def test_camel_case_splits(self):
        assert camel_to_title("facilityName") == "Facility Name"

    def test_underscore_preserved(self):
        # Distinct from humanize_field_name: separators are left literal so
        # established display keys keep their exact form.
        assert camel_to_title("utility_name") == "Utility_Name"

    def test_data_flattener_reuses_canonical_humanizer(self):
        from psweep.compilation import data_flattener

        assert data_flattener.humanize_field_name is humanize_field_name
