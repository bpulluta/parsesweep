"""Physical request guards count retries and never charge denied calls."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from psweep.discovery.attempt_budget import AttemptBudget, AttemptBudgetExceeded
from psweep.discovery.targets import render_target_template
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from psweep.extraction.llm_client import LLMClient


def test_thread_safe_review_reservations_are_per_target():
    budget = AttemptBudget(max_review_calls=3)

    def reserve(_index):
        try:
            budget.reserve_review("alpha")
        except AttemptBudgetExceeded:
            return False
        else:
            return True

    with ThreadPoolExecutor(max_workers=16) as executor:
        accepted = list(executor.map(reserve, range(1000)))
    assert sum(accepted) == 3
    budget.reserve_review("beta")
    assert budget.snapshot()["review_calls_per_target"] == {"alpha": 3, "beta": 1}


def test_search_retry_denial_preserves_attempt_count(monkeypatch):
    monkeypatch.setattr(SerpApiSeeker, "_ensure_prepared", lambda self: None)
    budget = AttemptBudget(max_search_calls=1)
    seeker = SerpApiSeeker(
        before_request=budget.reserve_search, retry_max_attempts=4, retry_initial_backoff_seconds=0
    )
    send = Mock(side_effect=TimeoutError("fixture"))
    with pytest.raises(AttemptBudgetExceeded):
        seeker._execute_search_with_retry(send, {})
    assert send.call_count == seeker.queries_run == budget.snapshot()["search_calls"] == 1


def test_llm_retry_denial_is_not_provider_failure(monkeypatch):
    budget = AttemptBudget(max_review_calls=1)
    client = LLMClient(model="gpt-fixture", provider="openai", api_key="fixture")
    send = Mock(side_effect=TimeoutError("fixture"))
    monkeypatch.setattr(client, "_complete", send)
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)
    with pytest.raises(AttemptBudgetExceeded):
        client.complete(
            [{"role": "user", "content": "test"}],
            before_request=lambda: budget.reserve_review("alpha"),
        )
    assert send.call_count == client.usage["calls"] == 1
    assert client.usage["unknown_cost_calls"] == 1


def test_initial_llm_denial_never_counts_as_call(monkeypatch):
    budget = AttemptBudget(max_review_calls=1)
    budget.reserve_review("alpha")
    client = LLMClient(model="gpt-fixture", provider="openai", api_key="fixture")
    send = Mock()
    monkeypatch.setattr(client, "_complete", send)
    with pytest.raises(AttemptBudgetExceeded):
        client.complete(
            [{"role": "user", "content": "test"}],
            before_request=lambda: budget.reserve_review("alpha"),
        )
    assert send.call_count == client.usage["calls"] == 0


@pytest.mark.parametrize(
    "template,target,expected",
    [
        ("{name} {address?}", {"name": "Alpha"}, "Alpha"),
        ("{name} {address}", {"name": "Alpha", "address": "1 Main"}, "Alpha 1 Main"),
        ("{name}", {"name": "literal {not-an-instruction}"}, "literal {not-an-instruction}"),
    ],
)
def test_shared_target_template(template, target, expected):
    assert render_target_template(template, target) == expected


@pytest.mark.parametrize("template", ["{address}", "{missing", "{a.b}"])
def test_invalid_templates_fail(template):
    with pytest.raises(ValueError):
        render_target_template(template, {})
