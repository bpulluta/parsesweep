"""Command-line entry point for ParseSweep.

The actual CLI is defined in :mod:`psweep.cli.app` (Typer). This module
exists so the ``psweep.cli.main`` module path still works for subprocess
calls and ``python -m psweep.cli.main`` invocations during the migration.

The ``cli`` name is re-exported for test compatibility — tests use the
Click CliRunner against this object.
"""

from psweep.cli.app import _cli as cli, main, app  # noqa: F401

if __name__ == "__main__":
    main()
