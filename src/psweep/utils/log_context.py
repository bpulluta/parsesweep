"""Name the document a log record belongs to.

Extraction runs documents on worker threads, so a line such as an LLM retry
warning is ambiguous unless it says which document it is about.
:func:`document_scope` sets that name for the current context and the CLI log
formatter prefixes it. Thread pools do not inherit context: submit work
through ``contextvars.copy_context().run`` (or wrap it in the scope).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_document: ContextVar[str | None] = ContextVar("psweep_document", default=None)


def current_document() -> str | None:
    """The document whose work is running in this context, if any."""
    return _document.get()


@contextmanager
def document_scope(name: str) -> Iterator[None]:
    """Attribute log records emitted in this scope to document ``name``.

    Also a decorator: ``pool.submit(document_scope(name)(func), ...)``.
    """
    token = _document.set(name)
    try:
        yield
    finally:
        _document.reset(token)
