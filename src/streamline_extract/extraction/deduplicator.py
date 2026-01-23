"""Universal document deduplication to avoid processing duplicate files.

Works with any document type by detecting duplicates via file hash,
content fingerprinting, and configurable ID extraction.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Set
import re

logger = logging.getLogger(__name__)


class DocumentDeduplicator:
    """
    Detect and skip duplicate or near-duplicate documents.
    
    Uses multiple strategies:
    1. File hash for exact duplicates
    2. Content fingerprint for near-duplicates (fuzzy matching)
    3. Document ID extraction for same-document different versions
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
        self.seen_document_ids: Dict[str, str] = {}  # document_id -> file_path
        
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
            
            # Check 3: Same document ID (draft vs final, revised versions)
            document_id = self._extract_document_id(text_content)
            if document_id:
                if document_id in self.seen_document_ids:
                    existing_file = self.seen_document_ids[document_id]
                    
                    # Decide which version to keep (prefer "final" over "draft")
                    if self._should_prefer_new_version(pdf_path, Path(existing_file)):
                        logger.info(
                            f"📝 Replacing {existing_file} with newer version: {pdf_path.name}"
                        )
                        # Remove old from cache
                        self._mark_as_duplicate(document_id)
                        return False, None
                    else:
                        logger.info(
                            f"⏭️  Skipping duplicate document {document_id}: {pdf_path.name} "
                            f"(already have {existing_file})"
                        )
                        return True, "document_id_duplicate"
        
        # Not a duplicate - record it
        self.seen_hashes.add(file_hash)
        
        if text_content:
            fingerprint = self._compute_content_fingerprint(text_content)
            self.seen_fingerprints.add(fingerprint)
            
            document_id = self._extract_document_id(text_content)
            if document_id:
                self.seen_document_ids[document_id] = str(pdf_path)
        
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
    
    def _extract_document_id(self, text: str) -> Optional[str]:
        """
        Extract document identifier from text.
        
        Looks for common ID patterns including:
        - "Permit No. 12345" / "Registration No: 67890"
        - "Document Number 11-ABC-123" / "File No. 456"
        - "Ordinance No. 2024-01" / "Tariff ID: ABC-123"
        - Any pattern like "[Type] No./Number/ID: [AlphaNumeric]"
        
        Universal patterns that work across document types.
        """
        patterns = [
            # Generic document identifiers
            r'(?i)(?:document|file|record|case)\s*(?:no\.?|number|id|#):?\s*([A-Z0-9\-\.]+)',
            # Permits and registrations
            r'(?i)(?:permit|registration|license)\s*(?:no\.?|number|#):?\s*([A-Z0-9\-]+)',
            # Ordinances and regulations
            r'(?i)(?:ordinance|regulation|code|chapter)\s*(?:no\.?|number|#):?\s*([A-Z0-9\-\.]+)',
            # Tariffs and schedules
            r'(?i)(?:tariff|schedule|rate)\s*(?:no\.?|number|id|#):?\s*([A-Z0-9\-\.]+)',
            # Generic patterns (must have some structure)
            r'(?i)(?:id|identifier)\s*(?:no\.?|number|#)?:?\s*([A-Z0-9]{2,}\-[A-Z0-9\-]+)',
            r'(?i)(?:no\.?|number)\s+([A-Z]{2,}\-?\d{4,})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                doc_id = match.group(1).strip()
                # Clean up common artifacts
                doc_id = doc_id.replace(' ', '').upper()
                if len(doc_id) >= 4:  # Reasonable ID length
                    return doc_id
        
        return None
    
    def _should_prefer_new_version(self, new_path: Path, old_path: Path) -> bool:
        """
        Decide which version of a document to keep.
        
        Prefers:
        1. "Final" over "Draft"
        2. "Revised"/"Amended" over original
        3. Newer date in filename
        4. Newer file modification time
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
    
    def _mark_as_duplicate(self, document_id: str):
        """Mark a document as replaced by newer version."""
        if document_id in self.seen_document_ids:
            # Remove from seen documents so new version can be added
            del self.seen_document_ids[document_id]
    
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
                # Support both old and new cache format
                self.seen_document_ids = cache_data.get('document_ids', cache_data.get('permits', {}))
                
                logger.info(
                    f"Loaded deduplication cache: {len(self.seen_hashes)} hashes, "
                    f"{len(self.seen_document_ids)} document IDs"
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
                'document_ids': self.seen_document_ids,
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
            'unique_document_ids': len(self.seen_document_ids),
        }
