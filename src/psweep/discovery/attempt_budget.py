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
        self._pre_reserved_remaining: dict[int, int] = {}
        self._total_committed = 0
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

    def pre_reserve_target_searches(self, target_idx: int, count: int) -> None:
        """Pre-allocate ``count`` search slots for one job, in dispatch order.

        Call once per seeker job, strictly in the order jobs will be dispatched
        (roster order, or slot-major order for a query-slot pass), so budget
        keeps going to earlier jobs even once dispatch itself becomes
        concurrent. ``target_idx`` may repeat across calls (once per slot); its
        allocation accumulates. The admission check is against a running total
        committed across every call ever made on this budget -- that total is
        never decremented, since every real search reserve_search() later
        counts was first admitted here and must not be charged twice.
        """
        with self._lock:
            total = self._total_committed + count
            if self.max_search_calls is not None and total > self.max_search_calls:
                raise AttemptBudgetExceeded("Search attempt budget exhausted for run")
            self._total_committed = total
            self._pre_reserved_remaining[target_idx] = (
                self._pre_reserved_remaining.get(target_idx, 0) + count
            )

    def reserve_search(self, target_idx: int | None = None) -> None:
        """Reserve one live search, including transient retries.

        Cache hits never call this. When ``target_idx`` is given and a budget
        is set, the unit is drawn strictly from that target's own allocation
        (see ``pre_reserve_target_searches``), so concurrent dispatch across
        targets can never race for the same budget unit.
        """
        with self._lock:
            if self.max_search_calls is None:
                self._searches += 1
                return
            if target_idx is not None:
                remaining = self._pre_reserved_remaining.get(target_idx, 0)
                if remaining <= 0:
                    raise AttemptBudgetExceeded("Search attempt budget exhausted for run")
                self._pre_reserved_remaining[target_idx] = remaining - 1
                self._searches += 1
                return
            if self._searches >= self.max_search_calls:
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
