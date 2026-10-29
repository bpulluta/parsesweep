"""Bounded discovery scheduling with caller-owned result ordering."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from typing import Any


def bounded_results(
    function: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    workers: int,
) -> Iterator[Any]:
    """Yield completed work with at most ``workers`` pending futures."""
    source = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {
            executor.submit(function, item) for item in islice(source, workers)
        }
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                yield future.result()
            pending.update(
                executor.submit(function, item)
                for item in islice(source, len(completed))
            )
