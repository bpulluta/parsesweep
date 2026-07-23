"""Configuration management for the ParseSweep toolkit."""

import os
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class Config:
    """
    Global configuration for the permit toolkit.

    Manages paths for data directories, schemas, API keys, and other settings.
    Can be initialized from environment variables or set programmatically.
    """

    def __init__(self, project_root: Optional[Path] = None):
        """
        Initialize configuration.

        Args:
            project_root: Root directory of the project (auto-detected if None)
        """
        if project_root is None:
            # Try to detect project root
            self.project_root = self._detect_project_root()
        else:
            self.project_root = Path(project_root)

        # Data directories - organized by state
        self.data_root = self.project_root / "data"
        self.permits_dir = self.data_root / "permits"
        self.extracted_dir = self.data_root / "extracted"
        self.outputs_dir = self.data_root / "outputs"

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
        - OpenAI (OPENAI_API_KEY)
        - Azure OpenAI (AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, etc.)
        - Anthropic Claude (ANTHROPIC_API_KEY)
        - Google Gemini (GEMINI_API_KEY, GOOGLE_API_KEY)
        - And more via LiteLLM

        Returns:
            dict with provider configuration
        """
        config = {
            "provider": None,
            "model": None,
            "api_key": None,
        }

        # Load from environment or .env file
        env_file = self.project_root / ".env"
        env_vars = {}

        # First, collect all env vars from .env file
        if env_file.exists():
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            env_vars[key.strip()] = value.strip()
                            # Also set in os.environ for LiteLLM
                            os.environ[key.strip()] = value.strip()
            except Exception as e:
                logger.warning(f"Error reading .env file: {e}")

        # Merge with actual environment variables (they take precedence)
        env_vars.update(os.environ)

        # Detect provider and model from environment
        if "AZURE_OPENAI_API_KEY" in env_vars and (
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
            config["model"] = env_vars.get("OPENAI_MODEL", "gpt-4o-mini")
            config["api_key"] = env_vars["OPENAI_API_KEY"]

        if config["provider"]:
            logger.info(
                f"Loaded LLM config: provider={config['provider']}, model={config['model']}"
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

    def setup_directories(self):
        """Create necessary directories if they don't exist."""
        directories = [
            self.data_root,
            self.permits_dir,
            self.extracted_dir,
            self.outputs_dir,
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")

    def get_permits_dir(self, state: str) -> Path:
        """Get permits directory for a specific state."""
        return self.data_root / state / "permits"

    def get_extracted_dir(self, state: str) -> Path:
        """Get extracted data directory for a specific state."""
        return self.data_root / state / "extracted"

    def get_visualizations_dir(self, state: str) -> Path:
        """Get visualizations directory for a specific state."""
        return self.data_root / state / "visualizations"

    def get_reports_dir(self, state: str) -> Path:
        """Get reports directory for a specific state."""
        return self.data_root / state / "reports"

    def __repr__(self) -> str:
        has_api_key = bool(self.llm_config.get("api_key"))
        return (
            f"Config(\n"
            f"  project_root={self.project_root},\n"
            f"  data_root={self.data_root},\n"
            f"  provider={self.llm_config.get('provider')},\n"
            f"  api_key={'***' if has_api_key else 'NOT SET'}\n"
            f")"
        )


# Global config instance (can be overridden)
_global_config = None


def get_config(project_root: Optional[Path] = None) -> Config:
    """
    Get the global configuration instance.

    Args:
        project_root: Optional project root (used only on first call)

    Returns:
        Config instance
    """
    global _global_config
    if _global_config is None:
        _global_config = Config(project_root)
    return _global_config


def set_config(config: Config):
    """Set the global configuration instance."""
    global _global_config
    _global_config = config
