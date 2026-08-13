"""Configuration management for the ParseSweep toolkit."""

import os
from pathlib import Path
from typing import Optional
import logging

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class Config:
    """
    Global configuration for the ParseSweep toolkit.

    Manages paths for schemas, API keys, and other settings.
    Can be initialized from environment variables or set programmatically.
    """

    def __init__(self, project_root: Optional[Path] = None):
        """
        Initialize configuration.

        Parameters
        ----------
        project_root : Optional[Path]
            Root directory of the project (auto-detected if None)
        """
        if project_root is None:
            # Try to detect project root
            self.project_root = self._detect_project_root()
        else:
            self.project_root = Path(project_root)

        # Schema path
        self.schema_dir = self.project_root / "schemas"
        self.default_schema = (
            self.schema_dir / "example_utility_rate_schema.json"
        )

        # Multi-provider LLM configuration
        self.llm_config = self._load_llm_config()

    def _load_llm_config(self) -> dict:
        """
        Load LLM provider configuration from environment.

        Supports:

        - Any OpenAI-compatible endpoint (LLM_BASE_URL + LLM_API_KEY) — highest priority
        - Azure OpenAI (AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, etc.)
        - Anthropic Claude (ANTHROPIC_API_KEY)
        - Google Gemini (GEMINI_API_KEY, GOOGLE_API_KEY)
        - OpenAI (OPENAI_API_KEY)

        Returns
        -------
        dict
            dict with provider configuration
        """
        config = {
            "provider": None,
            "model": None,
            "api_key": None,
        }

        # Load .env into os.environ via python-dotenv (does not override
        # existing environment variables), then read from os.environ so real
        # environment values take precedence over .env — a single, standard
        # dotenv path instead of a hand-rolled parser.
        env_file = self.project_root / ".env"
        if env_file.exists():
            try:
                load_dotenv(env_file, override=False)
            except Exception as e:
                logger.warning(f"Error reading .env file: {e}")

        env_vars = os.environ

        # Detect provider and model from environment.
        # Priority (highest → lowest):
        #   1. LLM_BASE_URL — generic OpenAI-compatible endpoint override.
        #      Works with any proxy (LiteLLM, OpenRouter, Azure AI Foundry, vLLM, …).
        #      Set this + LLM_API_KEY to route all calls through a single endpoint.
        #   2–5. Direct provider keys (Azure, Anthropic, Gemini, OpenAI).
        if "LLM_BASE_URL" in env_vars and "LLM_API_KEY" in env_vars:
            config["provider"] = "openai"  # OpenAI-compatible wire format
            config["model"] = env_vars.get("LLM_MODEL")
            config["api_key"] = env_vars["LLM_API_KEY"]
            config["base_url"] = env_vars["LLM_BASE_URL"]
        elif "AZURE_OPENAI_API_KEY" in env_vars and (
            "AZURE_OPENAI_MODEL" in env_vars
            or "AZURE_OPENAI_ENDPOINT" in env_vars
        ):
            config["provider"] = "azure"
            config["model"] = env_vars.get("AZURE_OPENAI_MODEL")
            config["api_key"] = env_vars["AZURE_OPENAI_API_KEY"]
            config["azure_endpoint"] = env_vars.get("AZURE_OPENAI_ENDPOINT")
            config["azure_api_version"] = env_vars.get(
                "AZURE_OPENAI_API_VERSION", "2024-02-15-preview"
            )
        elif "ANTHROPIC_API_KEY" in env_vars:
            config["provider"] = "anthropic"
            config["model"] = env_vars.get(
                "ANTHROPIC_MODEL", "claude-3.5-sonnet"
            )
            config["api_key"] = env_vars["ANTHROPIC_API_KEY"]
        elif "GEMINI_API_KEY" in env_vars or "GOOGLE_API_KEY" in env_vars:
            config["provider"] = "gemini"
            config["model"] = env_vars.get("GEMINI_MODEL", "gemini-1.5-pro")
            config["api_key"] = env_vars.get("GEMINI_API_KEY") or env_vars.get(
                "GOOGLE_API_KEY"
            )
        elif "OPENAI_API_KEY" in env_vars:
            config["provider"] = "openai"
            # "gpt-4o-mini" mirrors llm_factory.DEFAULT_MODEL; can't import it
            # here — config.py is upstream of llm_factory (cycle prevention).
            config["model"] = env_vars.get("OPENAI_MODEL", "gpt-4o-mini")
            config["api_key"] = env_vars["OPENAI_API_KEY"]

        if config["provider"]:
            base_url_suffix = f", base_url={config['base_url']}" if config.get("base_url") else ""
            logger.info(
                f"Loaded LLM config: provider={config['provider']}, model={config['model']}{base_url_suffix}"
            )
        else:
            logger.warning("No LLM provider configured in environment")

        return config

    def _detect_project_root(self) -> Path:
        """Detect project root directory."""
        # Start from current working directory
        current = Path.cwd()

        # Look for markers that indicate project root
        markers = ["pyproject.toml", "setup.py", ".git", "src"]

        # Check current directory and parents
        for parent in [current] + list(current.parents):
            if any((parent / marker).exists() for marker in markers):
                logger.debug(f"Detected project root: {parent}")
                return parent

        # Default to current directory
        logger.warning(f"Could not detect project root, using: {current}")
        return current

    def __repr__(self) -> str:
        has_api_key = bool(self.llm_config.get("api_key"))
        return (
            f"Config(\n"
            f"  project_root={self.project_root},\n"
            f"  provider={self.llm_config.get('provider')},\n"
            f"  api_key={'***' if has_api_key else 'NOT SET'}\n"
            f")"
        )


# Global config instance (can be overridden)
_global_config = None


def get_config(project_root: Optional[Path] = None) -> Config:
    """
    Get the global configuration instance.

    Parameters
    ----------
    project_root : Optional[Path]
        Optional project root (used only on first call)

    Returns
    -------
    Config
        Config instance
    """
    global _global_config
    if _global_config is None:
        _global_config = Config(project_root)
    return _global_config
