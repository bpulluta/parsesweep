"""Document deduplication to avoid processing duplicate permits."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Set
import re

logger = logging.getLogger(__name__)


class DocumentDeduplicator:
    """
    Detect and skip duplicate or near-duplicate permit documents.
    
    Uses multiple strategies:
    1. File hash for exact duplicates
    2. Content fingerprint for near-duplicates
    3. Permit number extraction for same-permit different versions
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        """
        Initialize deduplicator.
        
        Args:
            cache_dir: Directory to store deduplication cache
        """
        self.cache_dir = cache_dir
        self.seen_hashes: Set[str] = set()
        self.seen_fingerprints: Set[str] = set()
        self.seen_permits: Dict[str, str] = {}  # permit_number -> file_path
        
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_cache()
    
    def is_duplicate(
        self, 
        pdf_path: Path, 
        text_content: Optional[str] = None
    ) -> tuple[bool, Optional[str]]:
        """
        Check if document is a duplicate.
        
        Args:
            pdf_path: Path to PDF file
            text_content: Optional pre-extracted text content
            
        Returns:
            Tuple of (is_duplicate, reason)
        """
        # Check 1: Exact file hash
        file_hash = self._compute_file_hash(pdf_path)
        if file_hash in self.seen_hashes:
            logger.info(f"⏭️  Skipping exact duplicate: {pdf_path.name}")
            return True, "exact_duplicate"
        
        # Check 2: Content fingerprint (if text provided)
        if text_content:
            fingerprint = self._compute_content_fingerprint(text_content)
            if fingerprint in self.seen_fingerprints:
                logger.info(f"⏭️  Skipping near-duplicate (same content): {pdf_path.name}")
                return True, "content_duplicate"
            
            # Check 3: Same permit number (draft vs final, revised versions)
            permit_number = self._extract_permit_number(text_content)
            if permit_number:
                if permit_number in self.seen_permits:
                    existing_file = self.seen_permits[permit_number]
                    
                    # Decide which version to keep (prefer "final" over "draft")
                    if self._should_prefer_new_version(pdf_path, Path(existing_file)):
                        logger.info(
                            f"📝 Replacing {existing_file} with newer version: {pdf_path.name}"
                        )
                        # Remove old from cache
                        self._mark_as_duplicate(permit_number)
                        return False, None
                    else:
                        logger.info(
                            f"⏭️  Skipping duplicate permit {permit_number}: {pdf_path.name} "
                            f"(already have {existing_file})"
                        )
                        return True, "permit_duplicate"
        
        # Not a duplicate - record it
        self.seen_hashes.add(file_hash)
        
        if text_content:
            fingerprint = self._compute_content_fingerprint(text_content)
            self.seen_fingerprints.add(fingerprint)
            
            permit_number = self._extract_permit_number(text_content)
            if permit_number:
                self.seen_permits[permit_number] = str(pdf_path)
        
        # Save cache after each update
        if self.cache_dir:
            self._save_cache()
        
        return False, None
    
    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA-256 hash of file."""
        hasher = hashlib.sha256()
        
        with open(file_path, 'rb') as f:
            # Read in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
        
        return hasher.hexdigest()
    
    def _compute_content_fingerprint(self, text: str) -> str:
        """
        Compute content fingerprint for near-duplicate detection.
        
        Normalizes text to ignore minor differences (whitespace, OCR artifacts).
        """
        # Normalize text
        normalized = text.lower()
        
        # Remove excessive whitespace
        normalized = re.sub(r'\s+', ' ', normalized)
        
        # Remove page numbers and dates (common differences between versions)
        normalized = re.sub(r'\bpage \d+ of \d+\b', '', normalized)
        normalized = re.sub(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b', '', normalized)
        
        # Take first 2000 chars for fingerprint (header + key content)
        fingerprint_text = normalized[:2000]
        
        return hashlib.sha256(fingerprint_text.encode()).hexdigest()
    
    def _extract_permit_number(self, text: str) -> Optional[str]:
        """
        Extract permit number from text.
        
        Looks for common patterns like:
        - "Permit No. 12345"
        - "Registration No: 67890"
        - "Permit Number 11-ABC-123"
        """
        patterns = [
            r'(?i)(?:permit|registration)\s*(?:no\.?|number|#):?\s*([A-Z0-9\-]+)',
            r'(?i)permit\s+([A-Z]{2,}\-?\d{4,})',
            r'(?i)registration\s+([A-Z]{2,}\-?\d{4,})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                permit_num = match.group(1).strip()
                # Clean up common artifacts
                permit_num = permit_num.replace(' ', '').upper()
                if len(permit_num) >= 4:  # Reasonable permit number length
                    return permit_num
        
        return None
    
    def _should_prefer_new_version(self, new_path: Path, old_path: Path) -> bool:
        """
        Decide which version of a permit to keep.
        
        Prefers:
        1. "Final" over "Draft"
        2. "Revised" over original
        3. Newer date in filename
        """
        new_name = new_path.name.lower()
        old_name = old_path.name.lower()
        
        # Prefer final over draft
        if 'final' in new_name and 'draft' in old_name:
            return True
        if 'draft' in new_name and 'final' in old_name:
            return False
        
        # Prefer revised/amended over original
        if any(word in new_name for word in ['revised', 'amended', 'modification']):
            if not any(word in old_name for word in ['revised', 'amended', 'modification']):
                return True
        
        # Prefer newer based on file modification time
        try:
            new_mtime = new_path.stat().st_mtime
            old_mtime = old_path.stat().st_mtime
            return new_mtime > old_mtime
        except Exception:
            pass
        
        # Default: keep old (conservative)
        return False
    
    def _mark_as_duplicate(self, permit_number: str):
        """Mark a permit as replaced by newer version."""
        if permit_number in self.seen_permits:
            # Remove from seen permits so new version can be added
            del self.seen_permits[permit_number]
    
    def _load_cache(self):
        """Load deduplication cache from disk."""
        if not self.cache_dir:
            return
        
        cache_file = self.cache_dir / "dedup_cache.json"
        
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    cache_data = json.load(f)
                
                self.seen_hashes = set(cache_data.get('hashes', []))
                self.seen_fingerprints = set(cache_data.get('fingerprints', []))
                self.seen_permits = cache_data.get('permits', {})
                
                logger.info(
                    f"Loaded deduplication cache: {len(self.seen_hashes)} hashes, "
                    f"{len(self.seen_permits)} permits"
                )
            except Exception as e:
                logger.warning(f"Failed to load dedup cache: {e}")
    
    def _save_cache(self):
        """Save deduplication cache to disk."""
        if not self.cache_dir:
            return
        
        cache_file = self.cache_dir / "dedup_cache.json"
        
        try:
            cache_data = {
                'hashes': list(self.seen_hashes),
                'fingerprints': list(self.seen_fingerprints),
                'permits': self.seen_permits,
            }
            
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
            
        except Exception as e:
            logger.warning(f"Failed to save dedup cache: {e}")
    
    def get_stats(self) -> Dict[str, int]:
        """Get deduplication statistics."""
        return {
            'unique_files': len(self.seen_hashes),
            'unique_contents': len(self.seen_fingerprints),
            'unique_permits': len(self.seen_permits),
        }
