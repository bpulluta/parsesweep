from __future__ import annotations

import json
import re
from pathlib import Path

from psweep.discovery import DiscoveryEngine, DiscoveryRequest


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "digger_html" / name


def _extract_links(html_content: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    pattern = r'<a\s+href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>'
    for match in re.finditer(pattern, html_content, flags=re.IGNORECASE):
        url = match.group(1)
        text = match.group(2).strip()
        if url.startswith(("http://", "https://")):
            links.append({"url": url, "text": text})
    return links


def test_distributed_topology_with_static_fixture_links(tmp_path: Path):
    html = _fixture_path("static_ordinance_page.html").read_text(encoding="utf-8")
    links = _extract_links(html)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[link["url"] for link in links],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="distributed",
        allowed_domains=["chaffeecounty.org"],
        include_url_patterns=[r"\.pdf$"],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["routing"]["mode"] == "distributed"
    assert payload["lineage"]["routing"]["artifact_count"] >= 3
    assert all(
        candidate["url"].startswith("https://chaffeecounty.org/")
        for candidate in payload["candidates"]
    )
    assert all(candidate["url"].endswith(".pdf") for candidate in payload["candidates"])


def test_centralized_topology_with_index_fixture_links(tmp_path: Path):
    html = _fixture_path("centralized_index_sweep.html").read_text(encoding="utf-8")
    links = _extract_links(html)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://docs.county.gov/index.html"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="centralized",
        hub_pages=["https://docs.county.gov/index.html"],
        allowed_domains=["docs.county.gov"],
        include_url_patterns=[r"\.pdf$"],
        index_links=links,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["routing"]["mode"] == "centralized"
    assert payload["lineage"]["routing"]["artifact_count"] >= 5
    assert payload["lineage"]["routing"]["discovery_modes"] == ["centralized_index_sweep"]
    assert all(candidate["url"].startswith("https://docs.county.gov/") for candidate in payload["candidates"])
    assert all(candidate["url"].endswith(".pdf") for candidate in payload["candidates"])


def test_hybrid_topology_with_combined_fixture_sources(tmp_path: Path):
    hub_html = _fixture_path("centralized_index_sweep.html").read_text(encoding="utf-8")
    distributed_html = _fixture_path("static_ordinance_page.html").read_text(encoding="utf-8")
    hub_links = _extract_links(hub_html)
    distributed_links = _extract_links(distributed_html)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[link["url"] for link in distributed_links],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="hybrid",
        hub_pages=["https://docs.county.gov/index.html"],
        allowed_domains=["docs.county.gov", "chaffeecounty.org"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"geothermal|ordinance|chapter"],
        index_links=hub_links,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    urls = [candidate["url"] for candidate in payload["candidates"]]

    assert payload["lineage"]["routing"]["mode"] == "hybrid"
    assert payload["lineage"]["routing"]["centralized_candidate_count"] >= 1
    assert payload["lineage"]["routing"]["distributed_candidate_count"] >= 1
    assert any(url.startswith("https://docs.county.gov/") for url in urls)
    assert any(url.startswith("https://chaffeecounty.org/") for url in urls)
    assert all(url.endswith(".pdf") for url in urls)