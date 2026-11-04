"""HTTP transport with connection-time public-address enforcement."""

from __future__ import annotations

import ipaddress
import json
import socket
import os
import time
from numbers import Real
from collections.abc import Iterator
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NameResolutionError, NewConnectionError

from psweep.security import (
    ca_bundle, protected_mode, resolve_tls_verification, tls_context,
)


class NetworkPolicyError(RuntimeError):
    """Acquisition cannot satisfy the configured security boundary."""

    def __init__(self, url: str, reason: str, result: object = None) -> None:
        self.url = url
        self.result = result
        self.blocking_code = getattr(result, "blocking_code", None)
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.hostname or 'unknown-host'}"
        super().__init__(
            f"Discovery acquisition denied for {origin}: {reason}"
        )


def public_address(address: str) -> bool:
    """Reject non-global, multicast, scoped, and reserved addresses."""
    if "%" in address:
        return False
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_public_url(url: str) -> None:
    """Check URL syntax and literal addresses before policy gates."""
    if protected_mode():
        raise NetworkPolicyError(
            url, "public discovery is disabled in protected mode"
        )
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.hostname
    ):
        raise NetworkPolicyError(
            url, "an HTTP(S) URL without credentials is required"
        )
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not public_address(parsed.hostname):
        raise NetworkPolicyError(
            url, "non-public destinations are not permitted"
        )


def _connect_public(connection: HTTPConnection) -> socket.socket:
    """Resolve once and connect only to validated addresses."""
    host = connection.host
    try:
        addresses = socket.getaddrinfo(
            host, connection.port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise NameResolutionError(host, connection, exc) from exc
    if not addresses or any(
        not isinstance(row[4][0], str) or not public_address(row[4][0])
        for row in addresses
    ):
        msg = f"http://{host}"
        raise NetworkPolicyError(
            msg, "DNS returned a non-public or empty address set"
        )
    last_error = None
    for family, kind, protocol, _, address in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            if connection.timeout is None or isinstance(
                connection.timeout, Real
            ):
                sock.settimeout(connection.timeout)
            for option in connection.socket_options or []:
                sock.setsockopt(*option)
            if connection.source_address:
                sock.bind(connection.source_address)
            # Connect to the sockaddr without a second DNS lookup.
            sock.connect(address)
        except OSError as exc:
            sock.close()
            last_error = exc
        else:
            return sock
    raise NewConnectionError(
        connection, "Could not connect to an approved address"
    ) from last_error


class PublicHTTPConnection(HTTPConnection):
    """Open HTTP sockets only to validated public addresses."""

    def _new_conn(self) -> socket.socket:
        return _connect_public(self)


class PublicHTTPSConnection(HTTPSConnection):
    """Keep the TLS hostname while pinning the underlying socket."""

    def _new_conn(self) -> socket.socket:
        return _connect_public(self)


class PublicHTTPPool(HTTPConnectionPool):
    """Pool HTTP connections without global configuration changes."""

    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    """Pool HTTPS connections with address validation."""

    ConnectionCls = PublicHTTPSConnection


class PublicHTTPAdapter(HTTPAdapter):
    """Use private pool classes and verified system or private roots."""

    def init_poolmanager(self, *args, **kwargs):
        """Install public-only pools on this adapter."""
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": PublicHTTPPool,
            "https": PublicHTTPSPool,
        }

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        """Use approved private roots or system trust."""
        host, options = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        options["ssl_context"] = tls_context(verify=verify is not False)
        return host, options


class BoundedResponse:
    """Expose discovery response operations with bounded decoding."""

    def __init__(
        self, response: requests.Response, *, max_bytes: int, deadline: float
    ):
        self._response = response
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._content: bytes | None = None
        self._bytes_read = 0
        self.status_code = response.status_code
        self.headers = response.headers
        self.url = response.url
        self.cookies = response.cookies

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """Bound decoded bytes and elapsed time between socket reads."""
        if self._content is not None:
            yield self._content
            return
        try:
            while True:
                self._check_limits()
                chunk = self._response.raw.read1(
                    chunk_size, decode_content=True
                )
                self._bytes_read += len(chunk)
                self._check_limits()
                if not chunk:
                    break
                yield chunk
        except BaseException:
            self.close()
            raise

    def _check_limits(self) -> None:
        if time.monotonic() > self._deadline:
            msg = "Discovery response deadline exceeded"
            raise requests.Timeout(msg)
        if self._bytes_read > self._max_bytes:
            raise NetworkPolicyError(
                self.url, "decoded response exceeds the byte limit"
            )

    @property
    def content(self) -> bytes:
        """Read a size-limited body once."""
        if self._content is None:
            self._content = b"".join(self.iter_content())
        return self._content

    @property
    def text(self) -> str:
        """Decode a bounded response using its declared encoding."""
        return self.content.decode(
            self._response.encoding or "utf-8", errors="replace"
        )

    def json(self):
        """Decode a bounded JSON response."""
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        """Propagate unsuccessful HTTP status codes."""
        self._response.raise_for_status()

    def close(self) -> None:
        """Release the underlying response and connection."""
        self._response.close()


def request_get(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    verify: bool,
    stream: bool,
    allow_redirects: bool,
    cookies=None,
) -> BoundedResponse:
    """Send once; the caller owns redirects and response lifetime."""
    validate_public_url(url)
    verify = resolve_tls_verification("PSWEEP_SSL_VERIFY", verify)
    if allow_redirects:
        raise NetworkPolicyError(
            url, "redirects must pass the shared destination gate"
        )
    max_bytes = int(
        os.environ.get("PSWEEP_MAX_RESPONSE_BYTES", str(50 * 1024 * 1024))
    )
    if max_bytes <= 0:
        msg = "PSWEEP_MAX_RESPONSE_BYTES must be a positive integer"
        raise ValueError(msg)
    deadline = time.monotonic() + timeout
    with requests.Session() as session:
        # Proxies/netrc could bypass policy or add ambient credentials.
        session.trust_env = False
        adapter = PublicHTTPAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        response = session.get(
            url,
            headers=headers,
            timeout=timeout,
            verify=ca_bundle() if verify else False,
            stream=True,
            allow_redirects=allow_redirects,
            cookies=cookies,
        )
        bounded = BoundedResponse(
            response,
            max_bytes=max_bytes,
            deadline=deadline,
        )
        if not stream:
            _ = bounded.content
        return bounded
