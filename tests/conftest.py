"""Deterministic test configuration without installed-module substitution."""

import os
import ipaddress
import socket

import pytest

from _helpers import synthetic_evidence_schema, write_json


@pytest.fixture
def schema_path(tmp_path):
    """Write the self-contained evidence contract used by shared feature tests."""
    path = tmp_path / "schema.json"
    write_json(path, synthetic_evidence_schema())
    return path

# Force color OFF before psweep imports build any Rich console. CLI tests assert
# on plain-text error substrings (e.g. "Missing option '--config'"); when Rich
# emits ANSI codes those substrings get split and the assertions fail. Some
# environments (notably GitHub Actions) export FORCE_COLOR, so pin deterministic
# no-color output here rather than depending on TTY detection.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ.pop("FORCE_COLOR", None)

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
for _key in (
    "LLM_API_KEY", "LLM_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_API_KEY",
    "AZURE_API_BASE", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "SERPAPI_API_KEY", "SERPAPI_KEY", "OPENROUTER_API_KEY",
):
    os.environ.pop(_key, None)


def pytest_addoption(parser):
    """Require an explicit opt-in before contacting external test endpoints."""
    parser.addoption("--live", action="store_true", help="Run approved public endpoint smoke tests")


def pytest_configure(config):
    """Register test layers without requiring an additional runner."""
    for marker in ("unit", "contract", "integration", "live"):
        config.addinivalue_line("markers", f"{marker}: {marker} validation layer")


def pytest_collection_modifyitems(config, items):
    """Keep live endpoints out of ordinary PR tests."""
    if not config.getoption("--live"):
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="external endpoint requires --live"))


@pytest.fixture(autouse=True)
def isolated_test_network(monkeypatch, request):
    """Reject external sockets except in explicitly enabled live tests."""
    if request.node.get_closest_marker("live") and request.config.getoption("--live"):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def allowed(address):
        if not isinstance(address, tuple):
            return True
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return address[0] == "localhost"

    def guarded_connect(sock, address):
        if not allowed(address):
            raise RuntimeError("External network is disabled in ordinary tests")
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not allowed(address):
            raise RuntimeError("External network is disabled in ordinary tests")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
