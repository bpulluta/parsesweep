"""Small file primitives for atomic publication and content identity."""

import hashlib
import json
import os
import tempfile
import socket
import uuid
import stat
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterable, Iterator


class PublicationLockError(ValueError):
    """Another writer owns the output publication lock."""


@contextmanager
def publication_lock(path: Path) -> Iterator[None]:
    """Hold a persistent-inode kernel lock; process death releases ownership."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        msg = f"Unsafe publication lock: {path}"
        raise PublicationLockError(msg)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        msg = f"Unsafe publication lock: {path}"
        raise PublicationLockError(msg) from exc
    with os.fdopen(descriptor, "r+b") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (os.name == "posix" and info.st_uid != os.getuid())
        ):
            msg = f"Unsafe publication lock: {path}"
            raise PublicationLockError(msg)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                msg = f"Publication already active: {path}"
                raise PublicationLockError(msg) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                msg = f"Publication already active: {path}"
                raise PublicationLockError(msg) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "token": uuid.uuid4().hex,
                    }
                ).encode()
            )
            handle.flush()
            os.fsync(handle.fileno())
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def staged_file(
    path: Path,
    content: str | bytes | Iterable[str],
    *,
    prefix: str | None = None,
) -> Iterator[Path]:
    """Stage flushed bytes (or streamed text chunks) beside the destination."""
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
            if isinstance(content, (str, bytes)):
                content = (content,)
            for chunk in content:
                handle.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write(
    path: Path,
    content: str | bytes | Iterable[str],
    *,
    prefix: str | None = None,
) -> None:
    """Replace a file with staged bytes on the same filesystem."""
    with staged_file(path, content, prefix=prefix) as temporary:
        temporary.replace(path)


def atomic_json(path: Path, payload: object, *, prefix: str | None = None) -> None:
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
