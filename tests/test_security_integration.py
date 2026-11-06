"""Real local TLS and provider protocol checks with synthetic data only."""

import json
import socket
import ssl
import subprocess  # noqa: S404 - tests drive openssl and the CLI as real processes
import threading
import time
import csv
import os
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from psweep.discovery.network import send_get, NetworkPolicyError
from psweep.extraction.llm_client import LLMClient
from psweep.workspace import Workspace

pytestmark = pytest.mark.integration
PAYLOAD = {"items": [{"name": "synthetic", "value": 42}]}


@pytest.fixture
def tls_server(tmp_path):
    """Run a local HTTPS origin with an ephemeral, explicitly trusted test CA."""
    cert, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - openssl is resolved from the test environment PATH
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=fixture.example",
            "-addext",
            "subjectAltName=DNS:fixture.example,DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://127.0.0.1/private")
                self.end_headers()
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(body)
            calls.append(request)
            if request["model"] == "timeout-model":
                time.sleep(1.5)
                return
            if request["model"] == "unavailable-model":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps(
                {
                    "id": "synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(PAYLOAD),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], cert, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def local_transport(monkeypatch, port):
    """Replace only the physical socket destination; retain real TLS and HTTP."""
    monkeypatch.setattr(
        "psweep.discovery.transport._connect_public",
        lambda connection: socket.create_connection(("127.0.0.1", port), timeout=3),
    )


def test_real_tls_custom_ca_and_proxy_isolation(tls_server, monkeypatch):
    port, cert, calls = tls_server
    local_transport(monkeypatch, port)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response, attempts = send_get(f"https://fixture.example:{port}/ok", timeout=3)
    try:
        assert response.json() == {"ok": True}
        assert attempts == 1
        assert calls == ["/ok"]
    finally:
        response.close()


@pytest.mark.parametrize("hostname,trusted", [("fixture.example", False), ("wrong.example", True)])
def test_real_tls_rejects_unknown_roots_and_wrong_hostname(
    tls_server, monkeypatch, hostname, trusted
):
    port, cert, calls = tls_server
    local_transport(monkeypatch, port)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    if trusted:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    with pytest.raises(requests.exceptions.SSLError):
        send_get(f"https://{hostname}:{port}/ok", timeout=3)
    assert calls == []


def test_real_public_tls_opt_out_keeps_destination_restrictions(tls_server, monkeypatch):
    port, _cert, calls = tls_server
    local_transport(monkeypatch, port)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    with pytest.warns(RuntimeWarning, match="verification is disabled"):
        response, _ = send_get(f"https://fixture.example:{port}/ok", timeout=3, verify=False)
    try:
        assert response.json() == {"ok": True}
    finally:
        response.close()
    with pytest.warns(RuntimeWarning), pytest.raises(NetworkPolicyError):
        send_get(f"https://fixture.example:{port}/redirect", timeout=3, verify=False)
    assert calls == ["/ok", "/redirect"]


def test_real_model_tls_option_is_per_client(tls_server, monkeypatch):
    from psweep.exceptions import ExtractionError

    port, _cert, calls = tls_server
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("PSWEEP_SSL_VERIFY", "false")
    endpoint = f"https://localhost:{port}/v1"
    with pytest.warns(RuntimeWarning, match="verification is disabled"):
        insecure = LLMClient(
            model="gpt-4o-mini",
            api_key="synthetic",
            base_url=endpoint,
            timeout=1,
        )
    monkeypatch.setenv("PSWEEP_SSL_VERIFY", "true")
    secure = LLMClient(
        model="gpt-4o-mini",
        api_key="synthetic",
        base_url=endpoint,
        timeout=1,
    )
    assert insecure.extract("synthetic", {"type": "object"})["data"] == PAYLOAD
    with pytest.raises(ExtractionError):
        secure.extract("synthetic", {"type": "object"})
    assert len(calls) == 1
    # An untrusted certificate is terminal: no retry handshakes.
    assert secure.usage["calls"] == 1


@pytest.mark.parametrize("model", ["timeout-model", "unavailable-model"])
def test_model_transport_failure_does_not_hide_retries(tls_server, monkeypatch, caplog, model):
    from psweep.exceptions import ExtractionError
    from psweep.extraction.llm_client import RETRY_ATTEMPTS

    port, cert, calls = tls_server
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    monkeypatch.setattr("psweep.extraction.llm_client.compute_backoff", lambda *_, **__: 0)
    client = LLMClient(
        model=model,
        api_key="synthetic",
        base_url=f"https://localhost:{port}/v1",
        timeout=1,
    )
    with pytest.raises(ExtractionError), caplog.at_level("WARNING", logger="psweep"):
        client.extract("synthetic", {"type": "object"})
    # Transient failures are retried by the client's own bounded loop only;
    # every request the endpoint saw is counted.
    assert len(calls) == RETRY_ATTEMPTS
    assert client.usage["calls"] == client.usage["unknown_cost_calls"] == len(calls)
    # Every retry is announced with a sanitized reason, never the endpoint.
    retries = [r.getMessage() for r in caplog.records if "retrying in" in r.getMessage()]
    assert [m.rpartition("attempt ")[2] for m in retries] == ["2/4)", "3/4)", "4/4)"]
    if model == "timeout-model":
        assert all("did not finish within the 1s timeout;" in m for m in retries)
    assert not any("localhost" in m or "://" in m for m in retries)


def test_real_redirect_to_private_address_never_contacts_target(tls_server, monkeypatch):
    port, cert, calls = tls_server
    local_transport(monkeypatch, port)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    with pytest.raises(NetworkPolicyError):
        send_get(f"https://fixture.example:{port}/redirect", timeout=3)
    assert calls == ["/redirect"]


def test_real_protected_model_request_uses_only_approved_tls_endpoint(tls_server, monkeypatch):
    port, cert, calls = tls_server
    endpoint = f"https://fixture.example:{port}/v1"
    resolver = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **kw: resolver(
            "127.0.0.1" if host == "fixture.example" else host,
            *a,
            **kw,
        ),
    )
    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    monkeypatch.setenv("LLM_BASE_URL", endpoint)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    result = LLMClient(model="synthetic-model", api_key="synthetic", base_url=endpoint).extract(
        "synthetic document",
        {"type": "object"},
    )
    assert result["data"] == PAYLOAD
    assert len(calls) == 1
    assert calls[0]["model"] == "synthetic-model"


def test_real_protected_cli_pipeline_reuse_and_exports(tls_server, tmp_path):
    """Run actual CLI subprocesses through HTTPS inference and both export writers."""
    from openpyxl import load_workbook

    port, cert, calls = tls_server
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    tools = workspace / "bin"
    tools.mkdir()
    unrelated_pixi = tools / "pixi"
    unrelated_pixi.write_text("#!/bin/sh\nexit 97\n")
    unrelated_pixi.chmod(0o755)
    documents = workspace / "outputs" / "synthetic" / "discovered" / "curated"
    documents.mkdir(parents=True)
    (documents / "sample.txt").write_text(
        "Synthetic audit document. Sample measurement is 42 units."
    )
    schema = workspace / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "value"],
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "number"},
                            },
                        },
                    }
                },
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                    },
                    "identity": {"deduplication": {"key_fields": ["name"]}},
                },
            }
        )
    )
    config = workspace / "run.yaml"
    config.write_text(
        json.dumps(
            {
                "domain": "synthetic",
                "schema": str(schema),
            }
        )
    )
    env = {
        "PATH": os.pathsep.join((str(tools), str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "HOME": str(workspace),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PYTHON_DOTENV_DISABLED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PSWEEP_SECURITY_MODE": "protected",
        "REQUESTS_CA_BUNDLE": str(cert),
        "LLM_BASE_URL": f"https://localhost:{port}/v1",
        "LLM_MODEL": "gpt-4o-mini",
        "LLM_API_KEY": "synthetic-not-a-secret",
    }

    def run(*args, expected=0):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "psweep.cli.main", *args],
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    run("run", "--config", str(config), "--skip-discover", "--yes", "--quiet")
    assert len(calls) == 1
    stage_dirs = Workspace("synthetic")
    record_files = list((workspace / stage_dirs.extracted).glob("*.json"))
    assert len(record_files) == 1
    assert json.loads(record_files[0].read_text())["payload"] == PAYLOAD
    run("run", "--config", str(config), "--skip-discover", "--yes", "--quiet")
    assert len(calls) == 1, "Verified reuse must not send another model request"
    (csv_path,) = (workspace / stage_dirs.compiled).glob("*.csv")
    with csv_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]["name"] == "synthetic"
    assert float(rows[0]["value"]) == 42
    (xlsx,) = (workspace / stage_dirs.compiled).glob("*.xlsx")
    workbook = load_workbook(xlsx, read_only=True, data_only=True)
    assert any(42 in row for sheet in workbook for row in sheet.values)
    workbook.close()
    empty = documents / "empty.txt"
    empty.write_text("")
    run(
        "extract",
        str(empty),
        "--schema",
        str(schema),
        "--output",
        str(workspace / "empty-output"),
        "--quiet",
        expected=1,
    )
    assert len(calls) == 1
    for path in [*record_files, csv_path, xlsx]:
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.live
def test_verified_public_pdf_download():
    response, _ = send_get(
        "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        timeout=30,
    )
    try:
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
    finally:
        response.close()
