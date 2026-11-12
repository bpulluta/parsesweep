"""Thread-safe outbound-attempt reservations; cache hits require no reservation."""

from __future__ import annotations

from collections import Counter
from threading import Lock


from psweep.exceptions import AttemptBudgetExceeded


class AttemptBudget:
    """Limit physical search attempts per run and review attempts per target/run."""

    def __init__(self, *, max_review_calls: int | None = None, max_search_calls: int | None = None):
        for value in (max_review_calls, max_search_calls):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("Attempt limits must be positive integers or None")
        self.max_review_calls = max_review_calls
        self.max_search_calls = max_search_calls
        self._reviews: Counter[str] = Counter()
        self._searches = 0
        self._lock = Lock()

    def reserve_review(self, target_key: str) -> None:
        """Reserve one grading, escalation, deduplication or retry request."""
        with self._lock:
            if (
                self.max_review_calls is not None
                and self._reviews[target_key] >= self.max_review_calls
            ):
                raise AttemptBudgetExceeded("Review attempt budget exhausted for target")
            self._reviews[target_key] += 1

    def reserve_search(self) -> None:
        """Reserve one live search, including transient retries."""
        with self._lock:
            if self.max_search_calls is not None and self._searches >= self.max_search_calls:
                raise AttemptBudgetExceeded("Search attempt budget exhausted for run")
            self._searches += 1

    def snapshot(self) -> dict:
        """Read counters without admitting more work."""
        with self._lock:
            return {
                "search_calls": self._searches,
                "review_calls": sum(self._reviews.values()),
                "review_calls_per_target": dict(self._reviews),
            }
