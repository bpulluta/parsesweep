"""Small file primitives for atomic publication and content identity."""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


@contextmanager
def staged_file(
    path: Path, content: str | bytes, *, prefix: str | None = None
) -> Iterator[Path]:
    """Stage flushed bytes beside the destination; clean up on exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=prefix or f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                content.encode("utf-8")
                if isinstance(content, str)
                else content
            )
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write(
    path: Path, content: str | bytes, *, prefix: str | None = None
) -> None:
    """Replace a file with staged bytes on the same filesystem."""
    with staged_file(path, content, prefix=prefix) as temporary:
        temporary.replace(path)


def atomic_json(
    path: Path, payload: object, *, prefix: str | None = None
) -> None:
    """Serialize finite JSON completely, then atomically replace."""
    atomic_write(
        path,
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
        prefix=prefix,
    )


def artifact_sha256(path: Path) -> str:
    """Hash file content without retaining the artifact in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
