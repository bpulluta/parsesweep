"""Intelligent text optimization to reduce token usage while preserving data quality.

Universal optimizer that works with any document type by removing boilerplate
while preserving data-rich content.
"""

import re
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


class DocumentTextOptimizer:
    """
    Optimize document text for extraction by removing unnecessary content.
    
    Strategy: Identify and remove boilerplate, legal text, and redundant sections
    while preserving all data-rich content (numbers, specifications, key values).
    
    Fully universal - works with any document type.
    """
    
    # Patterns for sections with minimal extraction value
    # CONSERVATIVE - only remove truly safe sections
    BOILERPLATE_PATTERNS = [
        # Appeal procedures (usually long and standardized, rarely contains data)
        r'(?i)(?:right to appeal|appeal procedures)[\s\S]{0,600}?(?:administrative law judge|final decision)',
        
        # Signature blocks and footer information (safe to remove)
        r'(?i)(?:signed|executed) (?:this|on)[\s\S]{0,150}?(?:director|administrator|official)',
    ]
    
    # REMOVED AGGRESSIVE PATTERNS:
    # - "shall maintain" (might be near important sections)
    # - "pursuant to/in accordance with" (often near important specs)
    # - "monitoring/testing procedures" (could have important context)
    # - reporting requirements (might reference key data)
    
    # Section headers that signal high-value content (PRESERVE THESE)
    # NOTE: These are examples - should be customizable per schema in future
    HIGH_VALUE_SECTION_MARKERS = [
        'limit', 'rate', 'equipment', 'description',
        'source', 'fuel', 'capacity', 'horsepower', 'kilowatt',
        'specification', 'requirement', 'standard',
        'tons per year', 'lbs per hour', 'pounds per hour',
        'number', 'registration', 'name', 'address', 'identifier',
        'operating limit', 'hours per year', 'content'
    ]
    
    # Repetitive footer/header patterns that appear on every page
    REPEATED_HEADER_FOOTER_PATTERNS = [
        r'(?i)page \d+ of \d+',
        r'(?i)(?:document|file|record) (?:no\.?|number):? \d+\s*(?:\n|$)',  # If repeated on every page
        r'(?i)(?:draft|final) (?:version|copy)\s*(?:\n|$)',  # If repeated
        r'(?i)(?:issued|effective) (?:date|on):? [\d/\-]+\s*(?:\n|$)',  # If repeated
    ]
    
    def __init__(self, max_reduction_percent: float = 20):
        """
        Initialize optimizer.
        
        Args:
            max_reduction_percent: Maximum percentage of text to remove (safety limit)
                                   Default: 20% (conservative to preserve data quality)
        """
        self.max_reduction_percent = max_reduction_percent
    
    def optimize_text(self, text: str, target_chars: int = None) -> Tuple[str, dict]:
        """
        Optimize document text for extraction while preserving data quality.
        
        Args:
            text: Raw extracted document text
            target_chars: Optional target character count (will use smart truncation if needed)
            
        Returns:
            Tuple of (optimized_text, stats_dict)
        """
        original_length = len(text)
        stats = {
            'original_chars': original_length,
            'removed_boilerplate_chars': 0,
            'removed_repeated_chars': 0,
            'removed_whitespace_chars': 0,
            'final_chars': 0,
            'reduction_percent': 0,
            'sections_removed': []
        }
        
        # Step 1: Remove excessive repeated headers/footers (appear on every page)
        text, repeated_removed = self._remove_repeated_elements(text)
        stats['removed_repeated_chars'] = repeated_removed
        
        # Step 2: Normalize whitespace (multiple newlines, spaces)
        text, whitespace_removed = self._normalize_whitespace(text)
        stats['removed_whitespace_chars'] = whitespace_removed
        
        # Step 3: Remove low-value boilerplate sections
        text, boilerplate_stats = self._remove_boilerplate(text)
        stats['removed_boilerplate_chars'] = boilerplate_stats['chars_removed']
        stats['sections_removed'] = boilerplate_stats['sections_removed']
        
        # Step 4: Safety check - don't remove too much
        current_length = len(text)
        reduction_percent = ((original_length - current_length) / original_length) * 100
        
        if reduction_percent > self.max_reduction_percent:
            logger.warning(f"Reduction ({reduction_percent:.1f}%) exceeds safety limit ({self.max_reduction_percent}%), using less aggressive optimization")
            # Fall back to safer optimization
            text, stats = self._safe_optimize(text)
        
        # Step 5: Smart truncation if still too long
        if target_chars and len(text) > target_chars:
            text = self._smart_truncate(text, target_chars)
            logger.info(f"Applied smart truncation to reach {target_chars} chars")
        
        stats['final_chars'] = len(text)
        stats['reduction_percent'] = ((original_length - len(text)) / original_length) * 100
        
        logger.info(f"Text optimization: {original_length:,} → {len(text):,} chars ({stats['reduction_percent']:.1f}% reduction)")
        
        return text, stats
    
    def _remove_repeated_elements(self, text: str) -> Tuple[str, int]:
        """Remove headers/footers that repeat on every page."""
        original_length = len(text)
        
        # Find patterns that appear 3+ times (likely repeated on each page)
        for pattern in self.REPEATED_HEADER_FOOTER_PATTERNS:
            matches = list(re.finditer(pattern, text))
            if len(matches) >= 3:  # Appears on 3+ pages
                # Keep first occurrence, remove rest
                for match in matches[1:]:
                    text = text[:match.start()] + text[match.end():]
        
        chars_removed = original_length - len(text)
        if chars_removed > 0:
            logger.debug(f"Removed {chars_removed} chars of repeated headers/footers")
        
        return text, chars_removed
    
    def _normalize_whitespace(self, text: str) -> Tuple[str, int]:
        """Normalize excessive whitespace while preserving structure."""
        original_length = len(text)
        
        # Replace 4+ consecutive newlines with 2
        text = re.sub(r'\n{4,}', '\n\n', text)
        
        # Replace multiple spaces with single space
        text = re.sub(r' {3,}', ' ', text)
        
        # Remove trailing whitespace from lines
        text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
        
        chars_removed = original_length - len(text)
        if chars_removed > 0:
            logger.debug(f"Removed {chars_removed} chars of excessive whitespace")
        
        return text, chars_removed
    
    def _remove_boilerplate(self, text: str) -> Tuple[str, dict]:
        """Remove boilerplate sections with low extraction value."""
        original_length = len(text)
        sections_removed = []
        
        # Check each section for high-value content before removing
        for pattern in self.BOILERPLATE_PATTERNS:
            matches = list(re.finditer(pattern, text))
            
            for match in matches:
                section_text = match.group(0).lower()
                
                # Safety check: Does this section contain high-value markers?
                has_high_value = any(
                    marker in section_text 
                    for marker in self.HIGH_VALUE_SECTION_MARKERS
                )
                
                # Don't remove if it contains important data
                if has_high_value:
                    logger.debug(f"Preserving section with high-value content: {section_text[:50]}...")
                    continue
                
                # Safe to remove
                section_preview = section_text[:60].replace('\n', ' ')
                sections_removed.append(section_preview)
                
                # Replace with brief placeholder
                text = text[:match.start()] + '\n[administrative text removed]\n' + text[match.end():]
        
        chars_removed = original_length - len(text)
        
        stats = {
            'chars_removed': chars_removed,
            'sections_removed': sections_removed
        }
        
        if chars_removed > 0:
            logger.info(f"Removed {len(sections_removed)} boilerplate sections ({chars_removed} chars)")
        
        return text, stats
    
    def _safe_optimize(self, text: str) -> Tuple[str, dict]:
        """
        Fallback: More conservative optimization if main approach removes too much.
        Only removes the safest patterns.
        """
        original_length = len(text)
        
        # Only normalize whitespace and remove obvious repeated elements
        text, repeated_removed = self._remove_repeated_elements(text)
        text, whitespace_removed = self._normalize_whitespace(text)
        
        stats = {
            'original_chars': original_length,
            'removed_boilerplate_chars': 0,
            'removed_repeated_chars': repeated_removed,
            'removed_whitespace_chars': whitespace_removed,
            'final_chars': len(text),
            'reduction_percent': ((original_length - len(text)) / original_length) * 100,
            'sections_removed': []
        }
        
        return text, stats
    
    def _smart_truncate(self, text: str, target_chars: int) -> str:
        """
        Intelligently truncate text to target length while preserving key sections.
        
        Strategy:
        1. Identify high-value sections (data tables, specifications)
        2. Keep document header (first 20%)
        3. Prioritize sections with numbers and technical terms
        4. Keep minimal context from other sections
        """
        if len(text) <= target_chars:
            return text
        
        # Always keep the first part (document details, entity info)
        header_size = int(target_chars * 0.20)  # 20% for header
        header = text[:header_size]
        
        remaining_budget = target_chars - header_size - 100  # Reserve 100 for footer marker
        
        # Split remaining text into sections
        remaining_text = text[header_size:]
        sections = self._split_into_sections(remaining_text)
        
        # Score sections by value (presence of numbers, keywords)
        scored_sections = [
            (self._score_section(section), section) 
            for section in sections
        ]
        
        # Sort by score (highest first)
        scored_sections.sort(reverse=True, key=lambda x: x[0])
        
        # Take top sections until budget is exhausted
        selected_sections = []
        current_size = 0
        
        for score, section in scored_sections:
            if current_size + len(section) <= remaining_budget:
                selected_sections.append(section)
                current_size += len(section)
            elif score > 0.7:  # High-value section, try to fit partial
                available = remaining_budget - current_size
                if available > 200:  # Only if we can fit meaningful content
                    selected_sections.append(section[:available])
                    current_size += available
                    break
            else:
                break
        
        # Reconstruct text
        optimized = header + '\n\n'.join(selected_sections) + '\n\n[...remaining sections truncated...]'
        
        return optimized
    
    def _split_into_sections(self, text: str) -> List[str]:
        """Split text into logical sections (by paragraphs or headers)."""
        # Split on double newlines or section headers
        sections = re.split(r'\n\n+', text)
        return [s.strip() for s in sections if len(s.strip()) > 50]
    
    def _score_section(self, section: str) -> float:
        """
        Score a section's value for extraction (0.0 to 1.0).
        
        Higher scores = more likely to contain extractable data.
        """
        section_lower = section.lower()
        score = 0.0
        
        # Numeric content (values, measurements, quantities)
        numeric_density = len(re.findall(r'\d+\.?\d*', section)) / max(len(section), 1)
        score += min(numeric_density * 100, 0.4)  # Up to 0.4 points
        
        # High-value keywords (from HIGH_VALUE_SECTION_MARKERS)
        keyword_count = sum(
            1 for marker in self.HIGH_VALUE_SECTION_MARKERS 
            if marker in section_lower
        )
        score += min(keyword_count * 0.1, 0.3)  # Up to 0.3 points
        
        # Presence of units (strong signal for quantitative data)
        units = ['per', '/', 'hours', 'kw', 'hp', '%', 'rate']
        unit_count = sum(1 for unit in units if unit in section_lower)
        score += min(unit_count * 0.1, 0.2)  # Up to 0.2 points
        
        # Structured data indicators (tables, lists)
        structure_indicators = [':', '|', '\t', 'table', 'schedule']
        structure_count = sum(1 for indicator in structure_indicators if indicator in section_lower)
        score += min(structure_count * 0.05, 0.1)  # Up to 0.1 points
        
        return min(score, 1.0)
    
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count from character count.
        
        Rule of thumb: ~4 characters per token for English text.
        """
        return len(text) // 4
