"""Discovery failures remain visible to CLI and pipeline automation."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from psweep.cli.commands_discover import discover
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from discovery_helpers import FakeResponse, make_candidate


@pytest.mark.parametrize("flags", [[], ["-q"], ["-v"]])
@pytest.mark.parametrize(
    "status,errors,expected",
    [
        ("success", [], 0),
        ("failed", [{"stage": "discovery.download", "message": "HTTP403"}], 1),
        (
            "partial_failure",
            [{"stage": "discovery.download", "message": "HTTP403"}],
            1,
        ),
        ("empty", [], 2),
    ],
)
def test_cli_exit_matches_manifest_at_every_verbosity(
    monkeypatch,
    tmp_path,
    flags,
    status,
    errors,
    expected,
):
    """Quiet output cannot bypass recorded failure or empty results."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"status": status, "errors": errors, "notes": []})
    )
    monkeypatch.setattr(
        DiscoveryEngine,
        "run",
        lambda _self, _request: SimpleNamespace(
            manifest_path=manifest,
            documents_dir=tmp_path / "documents",
            run_id="fixture",
            curated_dir=None,
            curated_count=0,
            review_index_path=None,
        ),
    )
    result = CliRunner().invoke(discover, ["--query", "fixture", *flags])
    assert result.exit_code == expected, result.output
    if "-q" in flags:
        assert str(manifest) in result.output
    if expected:
        assert "Discovery complete" not in result.output


@pytest.mark.parametrize("body", ["not json", "[]", "{}"])
def test_bad_manifest_fails_closed(monkeypatch, tmp_path, body):
    """A missing result contract is not an empty successful manifest."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(body)
    monkeypatch.setattr(
        DiscoveryEngine,
        "run",
        lambda _self, _request: SimpleNamespace(
            manifest_path=manifest,
            run_id="fixture",
            documents_dir=tmp_path,
        ),
    )
    result = CliRunner().invoke(discover, ["--query", "fixture", "-q"])
    assert result.exit_code == 1
    assert "manifest" in result.output.lower()


def test_stream_failure_does_not_leave_final_document(monkeypatch, tmp_path):
    """Interrupted downloads cannot leave partial files for later discovery."""

    class BrokenStream(FakeResponse):
        def iter_content(self, chunk_size=65536):
            yield b"partial"
            raise OSError("stream disconnected")

    response = BrokenStream(url="https://example.org/doc.pdf")
    response.close = Mock()
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    request = DiscoveryRequest(
        domain="fixture",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=False,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    _, record, error, saved, _ = DiscoveryEngine()._download_single_candidate(
        idx=0,
        candidate=make_candidate(response.url),
        request=request,
        documents_dir=tmp_path,
        ssl_verify=False,
        retry_policy={
            "max_attempts": 1,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
        },
        rate_limiter=None,
    )
    assert record["status"] == "failed" and error and not saved
    assert not list(tmp_path.rglob("*.pdf"))
    response.close.assert_called_once()
