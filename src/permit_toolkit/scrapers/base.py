"""Base scraper class for state environmental agency websites."""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any
import json
from datetime import datetime

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """
    Abstract base class for state permit scrapers.
    
    Subclasses should implement state-specific scraping logic while
    inheriting common functionality like file naming, resume capability,
    and manifest generation.
    """
    
    def __init__(
        self,
        output_dir: Path,
        test_mode: bool = False,
        test_count: int = 5,
        resume: bool = True,
    ):
        """
        Initialize base scraper.
        
        Args:
            output_dir: Directory to save downloaded permits
            test_mode: If True, only process test_count permits
            test_count: Number of permits to process in test mode
            resume: If True, skip existing files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.test_mode = test_mode
        self.test_count = test_count
        self.resume = resume
        
        self.manifest: List[Dict[str, Any]] = []
        self.stats = {
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }
        
        # Setup logging
        self._setup_logging()
        
    def _setup_logging(self):
        """Configure logging for the scraper."""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
    @abstractmethod
    def get_permit_list(self) -> List[Dict[str, Any]]:
        """
        Get list of permits to download.
        
        Returns:
            List of permit metadata dictionaries
        """
        pass
    
    @abstractmethod
    def download_permit(self, permit_info: Dict[str, Any], output_path: Path) -> bool:
        """
        Download a single permit.
        
        Args:
            permit_info: Permit metadata dictionary
            output_path: Path to save the downloaded file
            
        Returns:
            True if successful, False otherwise
        """
        pass
    
    def should_skip_file(self, output_path: Path) -> bool:
        """Check if file should be skipped (already exists and resume is True)."""
        if self.resume and output_path.exists():
            logger.info(f"Skipping existing file: {output_path.name}")
            return True
        return False
    
    def save_manifest(self):
        """Save download manifest as JSON."""
        manifest_path = self.output_dir / "download_manifest.json"
        with open(manifest_path, 'w') as f:
            json.dump({
                "generated_at": datetime.now().isoformat(),
                "stats": self.stats,
                "files": self.manifest
            }, f, indent=2)
        logger.info(f"Manifest saved to: {manifest_path}")
    
    def run(self):
        """Execute the scraping process."""
        logger.info(f"Starting {self.__class__.__name__}")
        logger.info(f"Output directory: {self.output_dir}")
        
        # Get permit list
        permits = self.get_permit_list()
        self.stats["total"] = len(permits)
        
        if self.test_mode:
            permits = permits[:self.test_count]
            logger.info(f"TEST MODE: Processing only {len(permits)} permits")
        
        # Download each permit
        for i, permit_info in enumerate(permits, 1):
            logger.info(f"Processing {i}/{len(permits)}: {permit_info.get('id', 'unknown')}")
            
            output_path = self._get_output_path(permit_info)
            
            if self.should_skip_file(output_path):
                self.stats["skipped"] += 1
                continue
            
            try:
                success = self.download_permit(permit_info, output_path)
                if success:
                    self.stats["downloaded"] += 1
                    self.manifest.append({
                        "file": output_path.name,
                        "permit_info": permit_info,
                        "downloaded_at": datetime.now().isoformat(),
                    })
                else:
                    self.stats["failed"] += 1
            except Exception as e:
                logger.error(f"Error downloading permit: {e}")
                self.stats["failed"] += 1
        
        # Save manifest and summary
        self.save_manifest()
        self._print_summary()
    
    @abstractmethod
    def _get_output_path(self, permit_info: Dict[str, Any]) -> Path:
        """
        Generate output file path for a permit.
        
        Args:
            permit_info: Permit metadata dictionary
            
        Returns:
            Path object for output file
        """
        pass
    
    def _print_summary(self):
        """Print download summary statistics."""
        logger.info("\n" + "="*50)
        logger.info("DOWNLOAD SUMMARY")
        logger.info("="*50)
        logger.info(f"Total permits:      {self.stats['total']}")
        logger.info(f"Downloaded:         {self.stats['downloaded']}")
        logger.info(f"Skipped (existing): {self.stats['skipped']}")
        logger.info(f"Failed:             {self.stats['failed']}")
        logger.info("="*50)
