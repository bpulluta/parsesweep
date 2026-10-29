"""Metadata-scale scheduling stays bounded without remote acquisition."""

from concurrent.futures import Future

from psweep.discovery.concurrency import bounded_results


def test_pending_work_bound_at_one_hundred_thousand(monkeypatch):
    """A large input iterator never creates an equally large future queue."""
    active = 0
    peak = 0

    class Executor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, function, item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            future = Future()
            future.set_result(function(item))
            return future

    def wait(futures, return_when):
        nonlocal active
        done = {next(iter(futures))}
        active -= 1
        return done, futures - done

    monkeypatch.setattr(
        "psweep.discovery.concurrency.ThreadPoolExecutor", Executor
    )
    monkeypatch.setattr("psweep.discovery.concurrency.wait", wait)
    results = bounded_results(lambda value: value, range(100_000), workers=4)
    assert sum(results) == sum(range(100_000))
    assert peak == 4
