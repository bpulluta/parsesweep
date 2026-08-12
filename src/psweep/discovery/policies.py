"""Policy evaluation helpers for discovery network access."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# Single source of truth for the user-facing policy defaults. Referenced by the
# ``DiscoveryRequest`` dataclass, the CLI options, the CLI resolution fallbacks,
# and ``DiscoveryPolicyEvaluator.evaluate`` so every layer agrees on one value.
# ``warn`` surfaces robots.txt / ToS concerns without blocking a run.
DEFAULT_ROBOTS_POLICY_MODE = "warn"
DEFAULT_TOS_POLICY_MODE = "warn"


@dataclass(slots=True)
class PolicyCheckResult:
    """Outcome of evaluating discovery policy controls for a target URL."""

    allowed: bool = True
    blocking_code: str | None = None
    warning_codes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class DiscoveryPolicyEvaluator:
    """Evaluate robots.txt and manual ToS acknowledgement policy controls."""

    DEFAULT_USER_AGENT = "ParseSweep/2.0 (+discovery)"

    def __init__(self) -> None:
        self._robots_cache: dict[
            tuple[str, str, bool], tuple[RobotFileParser | None, str | None]
        ] = {}

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        normalized = str(mode or "ignore").strip().lower()
        if normalized in {"warn", "enforce"}:
            return normalized
        return "ignore"

    @staticmethod
    def _normalize_domains(domains: list[str] | None) -> list[str]:
        normalized: list[str] = []
        for domain in domains or []:
            value = str(domain or "").strip().lower()
            if value:
                normalized.append(value)
        return normalized

    @staticmethod
    def _host_matches_domains(host: str, domains: list[str]) -> bool:
        host_lower = (host or "").strip().lower()
        if not host_lower:
            return False
        return any(
            host_lower == domain or host_lower.endswith("." + domain)
            for domain in domains
        )

    def _load_robots_parser(
        self,
        *,
        url: str,
        ssl_verify: bool,
        request_headers: dict[str, str] | None,
    ) -> tuple[RobotFileParser | None, str | None]:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None, "invalid URL for robots evaluation"

        cache_key = (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            bool(ssl_verify),
        )
        if cache_key in self._robots_cache:
            return self._robots_cache[cache_key]

        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        try:
            import requests
        except ImportError as exc:
            result = (None, f"requests dependency unavailable: {exc}")
            self._robots_cache[cache_key] = result
            return result

        try:
            response = requests.get(
                robots_url,
                timeout=15,
                allow_redirects=True,
                verify=ssl_verify,
                headers=request_headers
                or {"User-Agent": self.DEFAULT_USER_AGENT},
            )
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code == 404:
                parser = RobotFileParser()
                parser.parse([])
                result = (parser, None)
                self._robots_cache[cache_key] = result
                return result
            response.raise_for_status()
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse((getattr(response, "text", "") or "").splitlines())
            result = (parser, None)
            self._robots_cache[cache_key] = result
            return result
        except Exception as exc:
            result = (None, str(exc))
            self._robots_cache[cache_key] = result
            return result

    def evaluate(
        self,
        *,
        url: str,
        ssl_verify: bool,
        request_headers: dict[str, str] | None = None,
        robots_policy_mode: str = DEFAULT_ROBOTS_POLICY_MODE,
        tos_policy_mode: str = DEFAULT_TOS_POLICY_MODE,
        acknowledged_tos_domains: list[str] | None = None,
    ) -> PolicyCheckResult:
        result = PolicyCheckResult()
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()

        normalized_tos_mode = self._normalize_mode(tos_policy_mode)
        normalized_ack_domains = self._normalize_domains(
            acknowledged_tos_domains
        )
        if normalized_tos_mode != "ignore" and not self._host_matches_domains(
            host, normalized_ack_domains
        ):
            message = f"Terms acknowledgement missing for host '{host or 'unknown-host'}'."
            if normalized_tos_mode == "enforce":
                result.allowed = False
                result.blocking_code = "tos_unacknowledged"
                result.messages.append(message)
                return result
            result.warning_codes.append("tos_unacknowledged")
            result.messages.append(message)

        normalized_robots_mode = self._normalize_mode(robots_policy_mode)
        if normalized_robots_mode == "ignore":
            return result

        parser, load_error = self._load_robots_parser(
            url=url,
            ssl_verify=ssl_verify,
            request_headers=request_headers,
        )
        if parser is None:
            message = (
                f"robots.txt could not be evaluated for host '{host or 'unknown-host'}': "
                f"{load_error or 'unknown error'}."
            )
            if normalized_robots_mode == "enforce":
                result.allowed = False
                result.blocking_code = "robots_unavailable"
                result.messages.append(message)
                return result
            result.warning_codes.append("robots_unavailable")
            result.messages.append(message)
            return result

        user_agent = (request_headers or {}).get(
            "User-Agent"
        ) or self.DEFAULT_USER_AGENT
        if parser.can_fetch(user_agent, url):
            return result

        message = f"robots.txt disallows discovery for '{url}'."
        if normalized_robots_mode == "enforce":
            result.allowed = False
            result.blocking_code = "robots_disallowed"
            result.messages.append(message)
            return result

        result.warning_codes.append("robots_disallowed")
        result.messages.append(message)
        return result
