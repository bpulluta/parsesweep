"""
Intelligent deduplication for permit data.

Handles multiple types of duplicates:
1. Same facility with multiple permit versions (amendments A, B, C)
2. Same facility with multiple permits over time (keep most recent or highest capacity)
3. OCR/extraction errors creating near-duplicates (0 vs O, address variations)
4. Exact duplicates from processing errors
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from difflib import SequenceMatcher


class PermitDeduplicator:
    """Intelligently deduplicate permit records."""
    
    def __init__(self, strategy: str = "most_recent_highest_capacity"):
        """
        Initialize deduplicator.
        
        Args:
            strategy: Deduplication strategy:
                - "most_recent_highest_capacity": Keep most recent permit, or highest capacity if same date
                - "highest_capacity": Keep permit with highest total capacity
                - "most_recent": Keep most recent permit by issue date
                - "all_unique": Keep all permits with different permit numbers
        """
        self.strategy = strategy
        
    def normalize_address(self, address: str) -> str:
        """
        Normalize address for fuzzy matching.
        
        Handles:
        - OCR errors (0 vs O, 1 vs l)
        - Address ranges (9333/9355 -> 9333)
        - Whitespace variations
        - Case differences
        - Common abbreviations
        """
        if not address:
            return ""
        
        addr = address.lower().strip()
        
        # Remove newlines and extra whitespace
        addr = addr.replace('\n', ' ').replace('\r', ' ')
        addr = ' '.join(addr.split())
        
        # Handle address ranges - "9333/9355" -> "9333", "1300-1700" -> "1300"
        addr = re.sub(r'(\d+)[/\-]\d+', r'\1', addr)
        
        # Remove common OCR artifacts
        addr = addr.replace('..', ' ')
        
        # Normalize street abbreviations
        addr = re.sub(r'\bstreet\b', 'st', addr)
        addr = re.sub(r'\bavenue\b', 'ave', addr)
        addr = re.sub(r'\broad\b', 'rd', addr)
        addr = re.sub(r'\bdrive\b', 'dr', addr)
        addr = re.sub(r'\blane\b', 'ln', addr)
        addr = re.sub(r'\bboulevard\b', 'blvd', addr)
        
        # Remove "Location:" prefix that sometimes appears
        addr = re.sub(r'^location:\s*', '', addr)
        
        # Remove county suffix if present (we compare county separately)
        addr = re.sub(r',?\s*\w+\s+county\b.*$', '', addr)
        
        # Normalize whitespace again
        addr = ' '.join(addr.split())
        
        return addr
    
    def normalize_facility_name(self, name: str) -> str:
        """Normalize facility name for comparison."""
        if not name:
            return ""
        
        name = name.lower().strip()
        
        # Remove common legal entity suffixes
        name = re.sub(r'\b(llc|inc|corp|ltd|co)\b\.?', '', name)
        
        # Remove extra whitespace
        name = ' '.join(name.split())
        
        return name
    
    def parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse date string to datetime object."""
        if not date_str or date_str == "Unknown":
            return None
        
        # Try common date formats
        formats = [
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%Y/%m/%d",
            "%d-%m-%Y",
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except (ValueError, TypeError):
                continue
        
        return None
    
    def address_similarity(self, addr1: str, addr2: str) -> float:
        """
        Calculate similarity score between two addresses.
        
        Returns:
            Score between 0.0 and 1.0, where 1.0 is exact match
        """
        if not addr1 or not addr2:
            return 0.0
        
        norm1 = self.normalize_address(addr1)
        norm2 = self.normalize_address(addr2)
        
        # Exact match after normalization
        if norm1 == norm2:
            return 1.0
        
        # Use sequence matcher for fuzzy matching
        return SequenceMatcher(None, norm1, norm2).ratio()
    
    def are_same_facility(
        self,
        permit1: Dict,
        permit2: Dict,
        address_threshold: float = 0.9,
    ) -> bool:
        """
        Determine if two permits are for the same facility.
        
        Considers:
        - Facility ID (exact match)
        - Address similarity (fuzzy match)
        - County match
        - Facility name similarity
        """
        # Check facility ID first (strongest signal)
        fid1 = permit1.get('facility_id', '')
        fid2 = permit2.get('facility_id', '')
        if fid1 and fid2 and fid1 == fid2:
            return True
        
        # Check address similarity
        addr_sim = self.address_similarity(
            permit1.get('address_raw', ''),
            permit2.get('address_raw', '')
        )
        
        # Same county and very similar address
        if (permit1.get('county') == permit2.get('county') and 
            addr_sim >= address_threshold):
            return True
        
        # Very high address similarity even without county match
        if addr_sim >= 0.95:
            return True
        
        return False
    
    def select_best_permit(self, permits: List[Dict]) -> Dict:
        """
        Select the best permit from a group of duplicates.
        
        Strategy depends on self.strategy:
        - most_recent_highest_capacity: Most recent, then highest capacity
        - highest_capacity: Highest total capacity
        - most_recent: Most recent issue date
        - all_unique: Should not be called (keep all)
        """
        if not permits:
            raise ValueError("No permits provided")
        
        if len(permits) == 1:
            return permits[0]
        
        if self.strategy == "highest_capacity":
            return max(permits, key=lambda p: p.get('total_capacity_kw', 0))
        
        elif self.strategy == "most_recent":
            # Filter permits with valid dates
            dated_permits = [
                p for p in permits 
                if self.parse_date(p.get('permit_date'))
            ]
            if not dated_permits:
                # Fall back to highest capacity if no dates
                return max(permits, key=lambda p: p.get('total_capacity_kw', 0))
            
            return max(
                dated_permits,
                key=lambda p: self.parse_date(p.get('permit_date'))
            )
        
        elif self.strategy == "most_recent_highest_capacity":
            # Parse dates for all permits
            permits_with_dates = []
            for p in permits:
                date = self.parse_date(p.get('permit_date'))
                permits_with_dates.append((p, date))
            
            # Sort by date (most recent first), then capacity (highest first)
            # None dates go to end
            sorted_permits = sorted(
                permits_with_dates,
                key=lambda x: (
                    x[1] if x[1] else datetime.min,  # Date (None becomes min)
                    x[0].get('total_capacity_kw', 0)  # Capacity
                ),
                reverse=True
            )
            
            return sorted_permits[0][0]
        
        # Default: return first permit
        return permits[0]
    
    def deduplicate_permits(self, permits: List[Dict]) -> List[Dict]:
        """
        Deduplicate a list of permits.
        
        Args:
            permits: List of permit dictionaries
            
        Returns:
            Deduplicated list of permits
        """
        if not permits:
            return []
        
        # Strategy: all_unique - keep all with different permit numbers
        if self.strategy == "all_unique":
            seen_permit_numbers = set()
            unique_permits = []
            for permit in permits:
                permit_num = permit.get('permit_number')
                if permit_num not in seen_permit_numbers:
                    seen_permit_numbers.add(permit_num)
                    unique_permits.append(permit)
            return unique_permits
        
        # Group permits by facility
        facility_groups: Dict[int, List[Dict]] = {}
        processed = [False] * len(permits)
        
        for i, permit in enumerate(permits):
            if processed[i]:
                continue
            
            # Start a new group
            group = [permit]
            processed[i] = True
            
            # Find all other permits for the same facility
            for j in range(i + 1, len(permits)):
                if processed[j]:
                    continue
                
                if self.are_same_facility(permit, permits[j]):
                    group.append(permits[j])
                    processed[j] = True
            
            facility_groups[i] = group
        
        # Select best permit from each group
        deduplicated = []
        for group in facility_groups.values():
            best = self.select_best_permit(group)
            deduplicated.append(best)
        
        return deduplicated
    
    def get_deduplication_report(
        self,
        original_permits: List[Dict],
        deduplicated_permits: List[Dict]
    ) -> Dict:
        """
        Generate a report on deduplication results.
        
        Returns:
            Dictionary with statistics and details
        """
        original_count = len(original_permits)
        deduplicated_count = len(deduplicated_permits)
        removed_count = original_count - deduplicated_count
        
        # Calculate total capacity
        original_capacity = sum(p.get('total_capacity_kw', 0) for p in original_permits)
        deduplicated_capacity = sum(p.get('total_capacity_kw', 0) for p in deduplicated_permits)
        
        # Count by state
        from collections import Counter
        original_states = Counter(p.get('state') for p in original_permits)
        deduplicated_states = Counter(p.get('state') for p in deduplicated_permits)
        
        return {
            'strategy': self.strategy,
            'original_count': original_count,
            'deduplicated_count': deduplicated_count,
            'removed_count': removed_count,
            'removal_percentage': (removed_count / original_count * 100) if original_count > 0 else 0,
            'original_capacity_kw': original_capacity,
            'deduplicated_capacity_kw': deduplicated_capacity,
            'original_states': dict(original_states),
            'deduplicated_states': dict(deduplicated_states),
        }
