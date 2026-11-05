"""Regressions for transport and spreadsheet trust boundaries."""

import socket
import os

import pandas as pd
import pytest
from openpyxl import load_workbook

from psweep.compilation.csv_exporter import CsvExporter
from psweep.compilation.excel_formatter import ExcelFormatter
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from psweep.discovery.engine import DiscoveryEngine
from psweep.discovery.network import NetworkPolicyError, send_get
from psweep.discovery.transport import PublicHTTPConnection, BoundedResponse
from psweep.discovery.policies import DiscoveryPolicyEvaluator


def test_protected_input_path_alias_stays_inside_workspace(tmp_path, monkeypatch):
    from psweep.security import validate_protected_paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    validate_protected_paths({"input_path": tmp_path / "documents"})
    with pytest.raises(ValueError, match="Protected input_path"):
        validate_protected_paths({"input_path": tmp_path.parent / "outside"})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/document",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/document",
        "http://[::1]/document",
        "http://[::ffff:127.0.0.1]/document",
        "http://[fe80::1]/document",
        "http://user:password@example.org/document",
    ],
)
def test_nonpublic_or_credential_url_is_rejected_before_connection(url, monkeypatch):
    """Untrusted URLs must never reach a socket."""
    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda *args: calls.append(args))
    with pytest.raises(NetworkPolicyError):
        send_get(url, timeout=0.1)
    assert calls == []


def test_tls_verification_defaults_on(monkeypatch):
    """Environment omission must not disable certificate authentication."""
    for key in (
        "DISCOVERY_SSL_VERIFY",
        "SERPAPI_SSL_VERIFY",
        "PSWEEP_SSL_VERIFY",
    ):
        monkeypatch.delenv(key, raising=False)
    assert DiscoveryEngine._downloads_ssl_verify() is True
    assert SerpApiSeeker._resolve_ssl_verify(None) is True


@pytest.mark.parametrize("value", ["false", "0", "off", "invalid"])
def test_insecure_tls_environment_fails_closed_in_protected_mode(monkeypatch, value):
    """Protected workloads cannot opt out of certificate authentication."""
    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    monkeypatch.setenv("DISCOVERY_SSL_VERIFY", value)
    with pytest.raises(ValueError, match="TLS"):
        DiscoveryEngine._downloads_ssl_verify()


@pytest.mark.parametrize(
    "setting", ["DISCOVERY_SSL_VERIFY", "SERPAPI_SSL_VERIFY", "PSWEEP_SSL_VERIFY"]
)
def test_public_tls_opt_out_is_explicit_and_warns(monkeypatch, setting):
    from psweep.security import resolve_tls_verification

    monkeypatch.setenv(setting, "false")
    with pytest.warns(RuntimeWarning, match="verification is disabled"):
        assert resolve_tls_verification(setting) is False


@pytest.mark.parametrize(
    "setting", ["DISCOVERY_SSL_VERIFY", "SERPAPI_SSL_VERIFY", "PSWEEP_SSL_VERIFY"]
)
def test_protected_tls_rejects_even_overridden_or_other_stage_opt_out(monkeypatch, setting):
    from psweep.security import resolve_tls_verification

    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    monkeypatch.setenv(setting, "false")
    with pytest.raises(ValueError, match="Protected mode requires TLS"):
        resolve_tls_verification("PSWEEP_SSL_VERIFY", explicit=True)


def test_tls_setting_precedence_and_invalid_values(monkeypatch):
    from psweep.security import resolve_tls_verification

    monkeypatch.setenv("PSWEEP_SSL_VERIFY", "false")
    monkeypatch.setenv("DISCOVERY_SSL_VERIFY", "true")
    assert resolve_tls_verification("DISCOVERY_SSL_VERIFY") is True
    with pytest.warns(RuntimeWarning):
        assert resolve_tls_verification("DISCOVERY_SSL_VERIFY", False) is False
    monkeypatch.setenv("DISCOVERY_SSL_VERIFY", "typo")
    with pytest.raises(ValueError, match="TLS"):
        resolve_tls_verification("DISCOVERY_SSL_VERIFY")


@pytest.mark.parametrize("value", ["=1+1", "+SUM(1,1)", "-SUM(1,1)", "@SUM(1,1)", "\t=1+1"])
def test_spreadsheet_strings_are_literal_and_numbers_remain_numeric(tmp_path, value):
    """Both display formats must treat untrusted formulas as evidence."""
    frame = pd.DataFrame({value: [value], "numeric": [-42]})
    xlsx = tmp_path / "evidence.xlsx"
    csv = tmp_path / "evidence.csv"
    ExcelFormatter().save(frame, xlsx)
    CsvExporter().save(frame, csv)
    book = load_workbook(xlsx)
    assert book.active["A1"].data_type == "s"
    assert book.active["A2"].data_type == "s"
    assert book.active["A2"].value == value
    assert book.active["B2"].value == -42
    book.close()
    import csv as csv_module

    with csv.open(newline="") as stream:
        rows = list(csv_module.reader(stream))
    assert rows[0][0] == "'" + value
    assert rows[1][0] == "'" + value
    assert rows[1][1] == "-42"


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["8.8.8.8", "127.0.0.1"],
        ["::ffff:10.0.0.1"],
    ],
)
def test_dns_answers_are_all_checked_before_any_socket(monkeypatch, addresses):
    """A public hostname cannot authorize a private or mixed DNS answer set."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 80),
            )
            for address in addresses
        ],
    )
    opened = []
    monkeypatch.setattr(socket, "socket", lambda *_args: opened.append(True))
    with pytest.raises(NetworkPolicyError):
        PublicHTTPConnection("allowed.example", port=80)._new_conn()
    assert opened == []


def test_validated_sockaddr_is_used_without_second_dns_lookup(monkeypatch):
    """Connection pinning closes the lookup/connect DNS-rebinding window."""
    lookups = []
    connected = []

    def resolve(*_args, **_kwargs):
        lookups.append(True)
        address = "8.8.8.8" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    class Socket:
        def settimeout(self, value):
            pass

        def setsockopt(self, *args):
            pass

        def connect(self, address):
            connected.append(address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *_args: Socket())
    PublicHTTPConnection("allowed.example", port=443)._new_conn()
    assert len(lookups) == 1
    assert connected == [("8.8.8.8", 443)]


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://outside.example/private"])
def test_robots_redirect_denial_is_not_downgraded_to_warning(monkeypatch, mode, target):
    """Robots rules cannot bypass destination or configured source restrictions."""
    from types import SimpleNamespace

    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=302,
            url=url,
            headers={"Location": target},
            close=lambda: None,
        )

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    with pytest.raises(NetworkPolicyError):
        DiscoveryPolicyEvaluator().evaluate(
            url="https://allowed.example/document",
            ssl_verify=True,
            robots_policy_mode=mode,
            tos_policy_mode="ignore",
            source_gate=lambda url: url.startswith("https://allowed.example/"),
        )
    assert calls == ["https://allowed.example/robots.txt"]


def test_decoded_response_limit_closes_stream():
    """Limits count decoded bytes, independent of compressed Content-Length."""
    import time
    from types import SimpleNamespace
    from unittest.mock import Mock

    raw = SimpleNamespace(read1=Mock(side_effect=[b"1234", b"5678"]))
    source = SimpleNamespace(
        raw=raw,
        status_code=200,
        headers={"Content-Length": "1"},
        url="https://example.org/document",
        cookies={},
        close=Mock(),
    )
    response = BoundedResponse(source, max_bytes=6, deadline=time.monotonic() + 10)
    with pytest.raises(NetworkPolicyError, match="byte limit"):
        list(response.iter_content())
    source.close.assert_called_once()


def test_elapsed_response_deadline_closes_stream():
    """A slowly progressing response cannot read indefinitely."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    import requests

    source = SimpleNamespace(
        status_code=200,
        headers={},
        url="https://example.org/document",
        cookies={},
        close=Mock(),
        raw=Mock(),
    )
    response = BoundedResponse(source, max_bytes=100, deadline=0)
    with pytest.raises(requests.Timeout):
        list(response.iter_content())
    source.raw.read1.assert_not_called()
    source.close.assert_called_once()


MiB = 1024 * 1024


def _stream(monkeypatch, reads, *, stream, **options):
    """Read a fake socket body through ``request_get`` (timeout 10 s)."""
    from discovery_helpers import patch_socket_stream
    from psweep.discovery.transport import request_get

    url = "https://example.org/file"
    patch_socket_stream(monkeypatch, {url: reads})
    response = request_get(
        url,
        headers={},
        timeout=10,
        verify=True,
        stream=stream,
        allow_redirects=False,
        **options,
    )
    return sum(len(chunk) for chunk in response.iter_content())


def test_file_download_streams_past_the_in_memory_limit(monkeypatch):
    """A 60 MiB body streams to disk under the file limit but is refused in memory."""
    from psweep.discovery.transport import ResponseTooLargeError, max_file_bytes

    body = [(0.0, b"x" * MiB)] * 60
    assert _stream(monkeypatch, body, stream=True, max_bytes=max_file_bytes()) == 60 * MiB
    monkeypatch.setenv("PSWEEP_MAX_RESPONSE_BYTES", str(100 * MiB))
    with pytest.raises(ResponseTooLargeError, match=f"{50 * MiB} bytes") as page:
        _stream(monkeypatch, body, stream=False)
    assert "PSWEEP_MAX_RESPONSE_BYTES" not in str(page.value)
    monkeypatch.setenv("PSWEEP_MAX_RESPONSE_BYTES", str(10 * MiB))
    with pytest.raises(ResponseTooLargeError, match=f"{10 * MiB} bytes.*PSWEEP_MAX_RESPONSE_BYTES"):
        _stream(monkeypatch, body, stream=True, max_bytes=max_file_bytes())


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_file_limit_override_must_be_a_positive_integer(monkeypatch, value):
    from psweep.discovery.transport import max_file_bytes

    monkeypatch.setenv("PSWEEP_MAX_RESPONSE_BYTES", value)
    with pytest.raises(ValueError, match="PSWEEP_MAX_RESPONSE_BYTES"):
        max_file_bytes()


def test_file_download_aborts_only_when_bytes_stop_arriving(monkeypatch):
    """Files use a stall timeout; in-memory responses keep the total deadline."""
    import requests

    progressing = [(9.0, b"x")] * 6
    assert _stream(monkeypatch, progressing, stream=True, max_bytes=MiB) == 6
    with pytest.raises(requests.Timeout, match="deadline"):
        _stream(monkeypatch, progressing, stream=False)
    with pytest.raises(requests.Timeout, match="no data for 10 s"):
        _stream(monkeypatch, [(1.0, b"x"), (11.0, b"x")], stream=True, max_bytes=MiB)


@pytest.mark.parametrize(
    "endpoint",
    [None, "http://approved.example/v1", "https://other.example/v1"],
)
def test_protected_model_requires_exact_approved_https_endpoint(monkeypatch, endpoint):
    from psweep.extraction.llm_client import LLMClient

    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    monkeypatch.setenv("LLM_BASE_URL", "https://approved.example/v1")
    with pytest.raises(ValueError, match="Protected"):
        LLMClient(model="synthetic", base_url=endpoint)


@pytest.mark.parametrize("mode", ["public", "protected"])
def test_browser_requires_external_isolation_and_is_never_protected(monkeypatch, mode):
    from psweep.discovery.browser import (
        BrowserSession,
        BrowserUnavailableError,
    )

    monkeypatch.setenv("PSWEEP_SECURITY_MODE", mode)
    if mode == "protected":
        monkeypatch.setenv("PSWEEP_BROWSER_ISOLATED", "1")
    else:
        monkeypatch.delenv("PSWEEP_BROWSER_ISOLATED", raising=False)
    with pytest.raises(BrowserUnavailableError, match="externally network-isolated"):
        BrowserSession()._start()


def test_model_clients_do_not_mutate_shared_credentials(monkeypatch):
    from psweep.extraction.llm_client import LLMClient

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-synthetic")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
    LLMClient(
        api_key="private-client-key",
        model="synthetic",
        base_url="https://other.example/v1",
    )
    assert os.environ["OPENAI_API_KEY"] == "ambient-synthetic"
    assert os.environ["OPENAI_BASE_URL"] == "https://ambient.example/v1"


def test_provider_failures_never_log_credentials_or_document_content(monkeypatch, caplog):
    import logging
    from psweep.extraction.llm_client import LLMClient
    from psweep.exceptions import ExtractionError

    secret = "synthetic-secret-document-and-key"  # noqa: S105 - synthetic test value

    def fail(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("psweep.extraction.llm_client.completion", fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(ExtractionError) as caught:
        LLMClient(model="synthetic", base_url="https://approved.example/v1").extract(
            secret,
            {"type": "object"},
        )
    assert secret not in caplog.text
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_publication_lock_never_modifies_linked_files(tmp_path, kind):
    """Lock metadata must not overwrite an existing data-file inode."""
    from psweep.utils.file_io import publication_lock

    original = tmp_path / "original.txt"
    original.write_text("KEEP ORIGINAL BYTES")
    lock = tmp_path / ".publication.lock"
    if kind == "symlink":
        lock.symlink_to(original)
    else:
        lock.hardlink_to(original)
    with pytest.raises(ValueError, match="Unsafe publication lock"), publication_lock(lock):
        pytest.fail("A linked data file must not become a lock")
    assert original.read_text() == "KEEP ORIGINAL BYTES"
