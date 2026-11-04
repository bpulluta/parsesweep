"""Fail-closed policy for network and model boundaries."""

import os
import ssl
import stat
import tempfile
import warnings
from pathlib import Path
from urllib.parse import urlsplit
import truststore


def protected_mode() -> bool:
    """Return whether approved-endpoint operation is selected."""
    mode = os.environ.get("PSWEEP_SECURITY_MODE", "public").strip().lower()
    if mode not in {"public", "protected"}:
        msg = "PSWEEP_SECURITY_MODE must be public or protected"
        raise ValueError(msg)
    return mode == "protected"


def resolve_tls_verification(
    setting: str, explicit: bool | None = None
) -> bool:
    """Resolve verified TLS or a warned public-only explicit opt-out."""
    if explicit is not None and type(explicit) is not bool:
        msg = "TLS verification must be a boolean"
        raise ValueError(msg)
    protected = protected_mode()
    settings: tuple[str, ...] = (setting, "PSWEEP_SSL_VERIFY")
    if protected:
        settings += ("DISCOVERY_SSL_VERIFY", "SERPAPI_SSL_VERIFY")
    parsed = []
    for name in dict.fromkeys(settings):
        value = os.environ.get(name)
        if value is None:
            continue
        token = value.strip().lower()
        if token not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
            msg = f"{name} must be a boolean TLS verification setting"
            raise ValueError(msg)
        parsed.append(token in {"true", "1", "yes", "on"})
    verify = explicit if explicit is not None else next(iter(parsed), True)
    if protected and (not verify or False in parsed):
        msg = (
            "Protected mode requires TLS verification. Remove false "
            "SSL_VERIFY settings and configure REQUESTS_CA_BUNDLE "
            "for an approved CA."
        )
        raise ValueError(msg)
    if not verify:
        warnings.warn(
            "TLS certificate verification is disabled for this public-mode "
            "request. Connections and credentials can be intercepted; prefer "
            "an approved REQUESTS_CA_BUNDLE. "
            "Never use this for sensitive data.",
            RuntimeWarning,
            stacklevel=2,
        )
    return verify


def ca_bundle() -> str | bool:
    """Resolve explicitly configured CA roots."""
    configured = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get(
        "SSL_CERT_FILE"
    )
    if configured:
        path = Path(configured)
        if not path.is_file():
            msg = "Configured TLS CA bundle is not a readable file"
            raise ValueError(msg)
        return str(path)
    return True


def tls_context(verify: bool = True) -> ssl.SSLContext:
    """Use approved/system roots or an explicit public-only opt-out."""
    if not resolve_tls_verification("PSWEEP_SSL_VERIFY", verify):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context
    bundle = ca_bundle()
    if isinstance(bundle, str):
        context = ssl.create_default_context(cafile=bundle)
    else:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def require_model_endpoint(base_url: str | None) -> None:
    """Restrict protected calls to the approved HTTPS endpoint."""
    if not protected_mode():
        return
    approved = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not approved or not base_url or base_url.rstrip("/") != approved:
        msg = (
            "Protected mode requires the exact LLM_BASE_URL endpoint; "
            "direct providers and endpoint overrides are not permitted"
        )
        raise ValueError(msg)
    parsed = urlsplit(approved)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or any(
            (
                parsed.username is not None,
                parsed.password is not None,
                parsed.query,
                parsed.fragment,
            )
        )
    ):
        msg = (
            "Protected LLM_BASE_URL must be an HTTPS endpoint "
            "without URL credentials"
        )
        raise ValueError(msg)


def prepare_protected_runtime() -> None:
    """Require a private POSIX workspace, caches, and temporaries."""
    if not protected_mode():
        return
    if os.name != "posix":
        msg = "Protected CLI operation requires POSIX permission checks"
        raise ValueError(msg)
    root = Path.cwd().resolve()
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        msg = "Protected mode requires an owner-only workspace (chmod 700)"
        raise ValueError(msg)
    for ancestor in root.parents:
        ancestor_info = ancestor.stat()
        mode = ancestor_info.st_mode
        if (
            ancestor_info.st_uid not in {0, os.getuid()}
            or (mode & 0o022 and not mode & stat.S_ISVTX)
        ):
            msg = "Protected workspace has an untrusted or writable ancestor"
            raise ValueError(msg)
    os.umask(0o077)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in (
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TIKTOKEN_CACHE_DIR",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ):
        path = Path(
            os.environ.get(name, str(root / ".psweep-private" / name.lower()))
        ).resolve()
        if not path.is_relative_to(root):
            msg = f"{name} must be inside the protected workspace"
            raise ValueError(msg)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            path.stat().st_uid != os.getuid()
            or stat.S_IMODE(path.stat().st_mode) & 0o077
        ):
            msg = f"{name} must be an owner-only directory"
            raise ValueError(msg)
        os.environ[name] = str(path)
    tempfile.tempdir = os.environ["TMPDIR"]
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def validate_protected_paths(values: dict) -> None:
    """Keep configured data and outputs within the private workspace."""
    if not protected_mode():
        return
    root = Path.cwd().resolve()
    for key in (
        "input_dir",
        "input_path",
        "documents_dir",
        "extracted_dir",
        "output",
        "output_dir",
        "validated_dir",
        "_extraction_input_dir",
    ):
        value = values.get(key)
        if value and not Path(value).resolve().is_relative_to(root):
            msg = f"Protected {key} must remain inside the private workspace"
            raise ValueError(msg)
