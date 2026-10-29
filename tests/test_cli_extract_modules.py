"""CLI registration is direct; workflow helpers have one UI-free owner."""

from psweep.cli import commands
from psweep.cli.app import _build_cli
from psweep.cli.commands_extract import extract
from psweep.pipeline import _extract_one_document


def test_extract_uses_direct_registration_without_private_facade():
    """Deleting reexports cannot silently drop the user-facing command."""
    assert _build_cli().commands["extract"] is extract
    assert _extract_one_document.__module__ == "psweep.pipeline"
    for name in (
        "extract",
        "_extract_one_document",
        "_extract_and_save_result",
        "_generate_run_id",
        "_resolve_schema_ref",
    ):
        assert not hasattr(commands, name)
