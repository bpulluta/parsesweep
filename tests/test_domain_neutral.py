"""Domain-neutral discovery: target fields drive templates and partitions."""

from __future__ import annotations

from pathlib import Path

from psweep.discovery.engine import DiscoveryEngine, DiscoveryRequest


def _request(**kw) -> DiscoveryRequest:
    return DiscoveryRequest(domain="d", dry_run=True, **kw)


class TestTargetTemplateContext:
    def test_no_implicit_domain_aliases(self):
        ctx = DiscoveryEngine._target_template_context(
            {"jurisdiction": "Boulder County", "manufacturer": "Generac"}
        )
        assert ctx == {"jurisdiction": "Boulder County", "manufacturer": "Generac"}

    def test_reserved_and_non_scalar_fields_are_excluded(self):
        ctx = DiscoveryEngine._target_template_context(
            {
                "county": "Adams",
                "query": "q",
                "urls": ["https://a.gov/x.pdf"],
                "blank": " ",
                "fips": 8001,
            }
        )
        assert ctx == {"county": "Adams", "fips": "8001"}


class TestPartitionBy:
    def test_generic_partition_path(self):
        req = _request(partition_by=["state", "county"])
        path = DiscoveryEngine._partition(req, "t1", {"state": "CO", "county": "Adams County"})
        assert path == Path("by_state_county/co/adams-county")

    def test_missing_field_uses_unknown_fallback(self):
        req = _request(partition_by=["state", "county"])
        assert "unknown-county" in str(DiscoveryEngine._partition(req, "t1", {"state": "CO"}))

    def test_default_target_and_shared_partitions(self):
        req = _request()
        assert DiscoveryEngine._partition(req, "t1", {"state": "CO"}) == Path("_targets/t1")
        assert DiscoveryEngine._partition(req, None, None) == Path("_shared")
