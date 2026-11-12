"""Every review entry point shares strict metadata rules and domain-authored templates."""

import pytest

from psweep.discovery.review_policy import iter_review_scopes, validate_review_policy


@pytest.mark.parametrize("context", ["name", ["name", "name"], ["urls"], ["query"], [123], [""]])
def test_invalid_context_fields_rejected(context):
    with pytest.raises(ValueError, match="context_fields"):
        validate_review_policy(context, {})


@pytest.mark.parametrize("coverage", [[], {"bad.label": "need"}, {"need": ""}, {1: "need"}])
def test_invalid_coverage_contract_rejected(coverage):
    with pytest.raises(ValueError, match="coverage"):
        validate_review_policy([], coverage)


def test_scope_binding_streams_and_does_not_mutate_templates():
    coverage = {"history": "History for {asset} at {location?}"}
    targets = iter([{"asset": "A", "location": None}, {"asset": "B", "location": "Bay 2"}])
    scopes = iter_review_scopes("Record for {asset}", ["location"], coverage, targets)
    assert next(scopes) == ("Record for A", {"history": "History for A at"})
    assert next(scopes) == ("Record for B", {"history": "History for B at Bay 2"})
    assert list(scopes) == []
    assert coverage == {"history": "History for {asset} at {location?}"}


def test_unknown_context_rejected_after_all_target_fields_are_checked():
    with pytest.raises(ValueError, match="Unknown review context_fields"):
        list(iter_review_scopes("Evidence", ["absent"], {}, [{"asset": "A"}]))


def test_additional_template_iterator_is_checked_for_every_target():
    scopes = iter_review_scopes(
        "Evidence",
        [],
        {},
        [{"asset": "A"}, {}],
        additional_templates=iter(["{asset}"]),
    )
    assert next(scopes) == ("Evidence", {})
    with pytest.raises(ValueError, match=r"targets\[1\].*asset"):
        next(scopes)
