"""
Extraction validation and cross-checking for hybrid approach.

This module provides validation between OpenAI direct extraction and 
LangExtract results to ensure accuracy and traceability.
"""

import logging
from typing import Dict, Any, Tuple, Optional
from difflib import SequenceMatcher
from datetime import datetime

logger = logging.getLogger(__name__)


class ExtractionValidator:
    """
    Cross-validates OpenAI and LangExtract extraction results.
    
    Provides:
    - Field-by-field comparison
    - Conflict detection and resolution
    - Confidence scoring
    - Complete audit trails
    """
    
    def __init__(self, similarity_threshold: float = 0.85):
        """
        Initialize validator.
        
        Args:
            similarity_threshold: Minimum string similarity (0-1) to consider a match
        """
        self.similarity_threshold = similarity_threshold
        logger.info(f"Initialized ExtractionValidator (similarity_threshold={similarity_threshold})")
    
    def validate_permit_details(
        self,
        openai_details: Dict[str, Any],
        langextract_details: Dict[str, Any],
        regex_details: Optional[Dict[str, Any]] = None,
        langextract_citations: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Validate permit details from multiple sources.
        
        Args:
            openai_details: Permit details from OpenAI extraction
            langextract_details: Permit details from LangExtract
            regex_details: Optional regex fallback results
            langextract_citations: Optional citation info from LangExtract
        
        Returns:
            {
                "merged_details": {...},  # Best values from all sources
                "validation_report": {
                    "agreements": [...],
                    "conflicts": [...],
                    "confidence_scores": {...}
                },
                "audit_trail": {...}  # Full trail per field
            }
        """
        merged = {}
        report = {
            "agreements": [],
            "conflicts": [],
            "confidence_scores": {},
            "total_fields": 0,
            "fields_with_high_confidence": 0,
            "fields_needing_review": 0
        }
        audit_trail = {}
        
        # Standard permit detail fields
        permit_fields = [
            'permitNumber', 'permitIssuanceDate', 'facilityName',
            'facilityAddress', 'facilityCounty', 'facilityCity',
            'permitteeName', 'permitteeAddress'
        ]
        
        for field in permit_fields:
            # Get values from each source
            openai_val = openai_details.get(field)
            langextract_val = langextract_details.get(field)
            regex_val = regex_details.get(field) if regex_details else None
            
            # Get citation if available
            citation = None
            if langextract_citations and field in langextract_citations:
                citation = langextract_citations[field]
            
            # Resolve and score
            result = self._resolve_field(
                field=field,
                openai_val=openai_val,
                langextract_val=langextract_val,
                regex_val=regex_val,
                citation=citation
            )
            
            # Build merged result
            merged[field] = result['final_value']
            audit_trail[field] = result
            
            # Update report
            report['confidence_scores'][field] = result['confidence']
            report['total_fields'] += 1
            
            if result['confidence'] >= 0.80:
                report['fields_with_high_confidence'] += 1
            
            if result['needs_review']:
                report['fields_needing_review'] += 1
            
            # Track agreements and conflicts
            if openai_val and langextract_val:
                agreement, _ = self._compare_values(openai_val, langextract_val)
                if agreement:
                    report['agreements'].append(field)
                else:
                    report['conflicts'].append({
                        'field': field,
                        'openai_value': openai_val,
                        'langextract_value': langextract_val,
                        'final_value': result['final_value'],
                        'source': result['source'],
                        'confidence': result['confidence']
                    })
        
        # Calculate summary statistics
        avg_confidence = (
            sum(report['confidence_scores'].values()) / len(report['confidence_scores'])
            if report['confidence_scores'] else 0.0
        )
        report['average_confidence'] = avg_confidence
        
        logger.info(
            f"Validation complete: {report['fields_with_high_confidence']}/{report['total_fields']} "
            f"high confidence, {len(report['conflicts'])} conflicts, "
            f"{report['fields_needing_review']} need review"
        )
        
        return {
            'merged_details': merged,
            'validation_report': report,
            'audit_trail': audit_trail,
            'timestamp': datetime.now().isoformat()
        }
    
    def _resolve_field(
        self,
        field: str,
        openai_val: Any,
        langextract_val: Any,
        regex_val: Any,
        citation: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Resolve a single field using priority rules.
        
        Returns:
            {
                "final_value": ...,
                "source": "...",  # Which source was used
                "confidence": 0.95,
                "needs_review": False,
                "openai_value": ...,
                "langextract_value": ...,
                "regex_value": ...,
                "citation": {...}
            }
        """
        # Normalize "Not specified" to None for comparison
        openai_val = None if openai_val == "Not specified" else openai_val
        langextract_val = None if langextract_val == "Not specified" else langextract_val
        
        # Check agreement between sources
        openai_langextract_agree = False
        similarity = 0.0
        
        if openai_val and langextract_val:
            openai_langextract_agree, similarity = self._compare_values(
                openai_val, langextract_val
            )
        
        # PRIORITY RULES (highest to lowest confidence)
        
        # Rule 1: All three sources agree (confidence: 1.0)
        if openai_langextract_agree and regex_val:
            regex_agree, _ = self._compare_values(openai_val, regex_val)
            if regex_agree:
                return {
                    'final_value': openai_val,
                    'source': 'all_sources_agree',
                    'confidence': 1.0,
                    'needs_review': False,
                    'openai_value': openai_val,
                    'langextract_value': langextract_val,
                    'regex_value': regex_val,
                    'citation': citation
                }
        
        # Rule 2: OpenAI and LangExtract agree (confidence: 0.95)
        if openai_langextract_agree:
            # Boost confidence if we also have citation
            confidence = 0.98 if citation else 0.95
            return {
                'final_value': openai_val,
                'source': 'openai_langextract_agree',
                'confidence': confidence,
                'needs_review': False,
                'openai_value': openai_val,
                'langextract_value': langextract_val,
                'regex_value': regex_val,
                'citation': citation
            }
        
        # Rule 3: LangExtract with citation (confidence: 0.85, prefer traceable)
        if citation and langextract_val:
            # Check if OpenAI significantly disagrees
            needs_review = False
            if openai_val and similarity < 0.5:
                needs_review = True  # Significant disagreement
            
            return {
                'final_value': langextract_val,
                'source': 'langextract_cited',
                'confidence': 0.85 if not needs_review else 0.70,
                'needs_review': needs_review,
                'openai_value': openai_val,
                'langextract_value': langextract_val,
                'regex_value': regex_val,
                'citation': citation
            }
        
        # Rule 4: OpenAI and regex agree (confidence: 0.80)
        if openai_val and regex_val:
            agree, _ = self._compare_values(openai_val, regex_val)
            if agree:
                return {
                    'final_value': openai_val,
                    'source': 'openai_regex_agree',
                    'confidence': 0.80,
                    'needs_review': False,
                    'openai_value': openai_val,
                    'langextract_value': langextract_val,
                    'regex_value': regex_val,
                    'citation': citation
                }
        
        # Rule 5: LangExtract and regex agree (confidence: 0.75)
        if langextract_val and regex_val:
            agree, _ = self._compare_values(langextract_val, regex_val)
            if agree:
                return {
                    'final_value': langextract_val,
                    'source': 'langextract_regex_agree',
                    'confidence': 0.75,
                    'needs_review': False,
                    'openai_value': openai_val,
                    'langextract_value': langextract_val,
                    'regex_value': regex_val,
                    'citation': citation
                }
        
        # Rule 6: Only OpenAI (confidence: 0.65, needs review)
        if openai_val:
            return {
                'final_value': openai_val,
                'source': 'openai_only',
                'confidence': 0.65,
                'needs_review': True,
                'openai_value': openai_val,
                'langextract_value': langextract_val,
                'regex_value': regex_val,
                'citation': citation
            }
        
        # Rule 7: Only LangExtract (confidence: 0.60)
        if langextract_val:
            return {
                'final_value': langextract_val,
                'source': 'langextract_only',
                'confidence': 0.60,
                'needs_review': True,
                'openai_value': openai_val,
                'langextract_value': langextract_val,
                'regex_value': regex_val,
                'citation': citation
            }
        
        # Rule 8: Only regex (confidence: 0.50, needs review)
        if regex_val:
            return {
                'final_value': regex_val,
                'source': 'regex_only',
                'confidence': 0.50,
                'needs_review': True,
                'openai_value': openai_val,
                'langextract_value': langextract_val,
                'regex_value': regex_val,
                'citation': citation
            }
        
        # Rule 9: No value found (confidence: 0.20)
        return {
            'final_value': "Not specified",
            'source': 'no_extraction',
            'confidence': 0.20,
            'needs_review': True,
            'openai_value': openai_val,
            'langextract_value': langextract_val,
            'regex_value': regex_val,
            'citation': citation
        }
    
    def _compare_values(self, val1: Any, val2: Any) -> Tuple[bool, float]:
        """
        Compare two values with fuzzy matching.
        
        Returns:
            (is_match, similarity_score)
        """
        # Both None
        if val1 is None and val2 is None:
            return True, 1.0
        
        # One None
        if val1 is None or val2 is None:
            return False, 0.0
        
        # String comparison with fuzzy matching
        if isinstance(val1, str) and isinstance(val2, str):
            val1_norm = val1.strip().lower()
            val2_norm = val2.strip().lower()
            
            # Exact match
            if val1_norm == val2_norm:
                return True, 1.0
            
            # Fuzzy match using sequence matcher
            similarity = SequenceMatcher(None, val1_norm, val2_norm).ratio()
            return similarity >= self.similarity_threshold, similarity
        
        # Numeric comparison
        if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
            if abs(val1 - val2) < 0.01:
                return True, 1.0
            else:
                # Calculate relative difference
                max_val = max(abs(val1), abs(val2))
                if max_val > 0:
                    diff = abs(val1 - val2) / max_val
                    similarity = 1.0 - min(diff, 1.0)
                    return False, similarity
                return False, 0.0
        
        # Type mismatch
        return False, 0.0
    
    def format_validation_summary(self, validation_result: Dict[str, Any]) -> str:
        """
        Format validation result as human-readable summary.
        
        Args:
            validation_result: Output from validate_permit_details()
        
        Returns:
            Formatted string summary
        """
        report = validation_result['validation_report']
        
        summary = []
        summary.append("=" * 70)
        summary.append("VALIDATION SUMMARY")
        summary.append("=" * 70)
        summary.append(f"Total fields: {report['total_fields']}")
        summary.append(f"High confidence (≥0.80): {report['fields_with_high_confidence']}")
        summary.append(f"Average confidence: {report['average_confidence']:.1%}")
        summary.append(f"Agreements: {len(report['agreements'])}")
        summary.append(f"Conflicts: {len(report['conflicts'])}")
        summary.append(f"Needs review: {report['fields_needing_review']}")
        summary.append("")
        
        if report['conflicts']:
            summary.append("CONFLICTS:")
            for conflict in report['conflicts']:
                summary.append(f"  • {conflict['field']}:")
                summary.append(f"    OpenAI: {conflict['openai_value']}")
                summary.append(f"    LangExtract: {conflict['langextract_value']}")
                summary.append(f"    → Resolution: {conflict['final_value']} "
                             f"(source: {conflict['source']}, "
                             f"confidence: {conflict['confidence']:.1%})")
        
        summary.append("=" * 70)
        return "\n".join(summary)


def create_validator(similarity_threshold: float = 0.85) -> ExtractionValidator:
    """
    Factory function to create validator instance.
    
    Args:
        similarity_threshold: Minimum similarity for fuzzy string matching
    
    Returns:
        Configured ExtractionValidator instance
    """
    return ExtractionValidator(similarity_threshold=similarity_threshold)
