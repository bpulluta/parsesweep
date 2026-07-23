"""Tests for domain-neutral discovery config: query aliases + partitioning.

These replace previously hard-coded domain vocabulary (utility_or_jurisdiction
synthesis and by_jurisdiction/<state>/<jurisdiction> partitioning) with
config-driven, domain-agnostic mechanisms.
"""

from __future__ import annotations

from pathlib import Path

from psweep.discovery.engine import (
    DiscoveryEngine,
    DiscoveryRequest,
)


def _request(**kw) -> DiscoveryRequest:
    base = dict(
        domain="d",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=True,
    )
    base.update(kw)
    return DiscoveryRequest(**base)


class TestQueryContextAliases:
    def test_default_preserves_legacy_jurisdiction(self):
        ctx = DiscoveryEngine._target_template_context(
            {"jurisdiction": "Boulder County"}
        )
        assert ctx["utility_or_jurisdiction"] == "Boulder County"

    def test_default_preserves_legacy_manufacturer(self):
        ctx = DiscoveryEngine._target_template_context(
            {"manufacturer": "Generac"}
        )
        assert ctx["utility_or_jurisdiction"] == "Generac"

    def test_custom_alias_first_source_wins(self):
        ctx = DiscoveryEngine._target_template_context(
            {"county": "Adams", "st": "CO"}, {"place": ["county", "st"]}
        )
        assert ctx["place"] == "Adams"

    def test_custom_alias_falls_through_to_second(self):
        ctx = DiscoveryEngine._target_template_context(
            {"st": "CO"}, {"place": ["county", "st"]}
        )
        assert ctx["place"] == "CO"

    def test_alias_absent_when_no_source_present(self):
        ctx = DiscoveryEngine._target_template_context(
            {"unrelated": "x"}, {"place": ["county", "st"]}
        )
        assert "place" not in ctx


class TestPartitionBy:
    def test_generic_partition_path_and_meta(self):
        req = _request(partition_by=["state", "county"])
        mode, meta, path = DiscoveryEngine._resolve_partition_dir(
            documents_dir=Path("/docs"),
            url="https://x.com/a.pdf",
            request=req,
            target_metadata={"state": "CO", "county": "Adams County"},
        )
        assert mode == "fields"
        assert meta == {
            "source_state": "co",
            "source_county": "adams-county",
        }
        assert path == Path("/docs/by_state_county/co/adams-county")

    def test_missing_field_uses_unknown_fallback(self):
        req = _request(partition_by=["state", "county"])
        _, _, path = DiscoveryEngine._resolve_partition_dir(
            documents_dir=Path("/docs"),
            url="https://x.com/a.pdf",
            request=req,
            target_metadata={"state": "CO"},
        )
        assert "unknown-county" in str(path)

    def test_backcompat_host_mode_still_works(self):
        req = _request(partition_mode="host")
        mode, _, path = DiscoveryEngine._resolve_partition_dir(
            documents_dir=Path("/docs"),
            url="https://sub.example.com/a.pdf",
            request=req,
        )
        assert mode == "host"
        assert "by_host" in str(path)

    def test_backcompat_jurisdiction_mode_still_works(self):
        req = _request(partition_mode="jurisdiction", state="CO", jurisdiction="Boulder")
        mode, meta, path = DiscoveryEngine._resolve_partition_dir(
            documents_dir=Path("/docs"),
            url="https://x.com/a.pdf",
            request=req,
        )
        assert mode == "jurisdiction"
        assert "by_jurisdiction" in str(path)
        assert "source_jurisdiction" in meta
