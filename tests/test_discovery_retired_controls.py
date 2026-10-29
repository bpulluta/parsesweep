"""Unused discovery controls are rejected rather than silently accepted."""

import pytest

from psweep.config import RuntimeConfigError, resolve_command_config


def test_document_review_action_is_retired():
    """Review always retains originals and materializes selected curation."""
    with pytest.raises(RuntimeConfigError, match=r"document_review\.action"):
        resolve_command_config(
            command="discover",
            cli_values={},
            strict=True,
            config_data={
                "discovery": {
                    "queries": ["documents"],
                    "document_review": {"action": "move"},
                }
            },
        )
