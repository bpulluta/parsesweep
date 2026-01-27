"""Configuration management for the StreamlineExtract toolkit."""

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
        self.default_schema = self.schema_dir / "example_utility_rate_schema.json"
        
        # API configuration
        self.openai_api_key = self._load_api_key()
        
    def _detect_project_root(self) -> Path:
        """Detect project root directory."""
        # Start from current working directory
        current = Path.cwd()
        
        # Look for markers that indicate project root
        markers = ['pyproject.toml', 'setup.py', '.git', 'src']
        
        # Check current directory and parents
        for parent in [current] + list(current.parents):
            if any((parent / marker).exists() for marker in markers):
                logger.debug(f"Detected project root: {parent}")
                return parent
        
        # Default to current directory
        logger.warning(f"Could not detect project root, using: {current}")
        return current
    
    def _load_api_key(self) -> Optional[str]:
        """Load OpenAI API key from environment or .env file."""
        # Check environment variable first
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            return api_key
        
        # Try loading from .env file
        env_file = self.project_root / ".env"
        if env_file.exists():
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            if key.strip() == 'OPENAI_API_KEY':
                                api_key = value.strip()
                                os.environ['OPENAI_API_KEY'] = api_key
                                logger.debug("Loaded API key from .env file")
                                return api_key
            except Exception as e:
                logger.warning(f"Error reading .env file: {e}")
        
        return None
    
    def setup_directories(self):
        """Create necessary directories if they don't exist."""
        directories = [
            self.data_dir,
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
        return (
            f"Config(\n"
            f"  project_root={self.project_root},\n"
            f"  data_dir={self.data_dir},\n"
            f"  api_key={'***' if self.openai_api_key else 'NOT SET'}\n"
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
