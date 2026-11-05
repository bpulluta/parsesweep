"""Module execution entry point for ``python -m psweep.cli.main``."""

from psweep.cli.app import cli, main  # noqa: F401 - re-exported for callers and tests

if __name__ == "__main__":
    main()
