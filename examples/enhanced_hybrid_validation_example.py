"""
Enhanced Hybrid Extraction - Implementation Example

Demonstrates the three-layer validation approach:
1. OpenAI Direct (primary extraction)
2. LangExtract (traceability & verification)
3. Cross-validation & merging

This is a PROOF-OF-CONCEPT showing how the enhanced approach would work.
"""

from pathlib import Path
from typing import Dict, Any, List, Tuple
import json
from difflib import SequenceMatcher
from datetime import datetime


class ExtractionValidator:
    """
    Cross-validates OpenAI and LangExtract results.
    
    Compares two extraction sources and produces:
    - Merged result with best values
    - Validation report showing agreements/conflicts
    - Complete audit trail with citations and confidence scores
    """
    
    def __init__(self, similarity_threshold: float = 0.85):
        """
        Initialize validator.
        
        Args:
            similarity_threshold: Minimum string similarity (0-1) to consider a match
        """
        self.similarity_threshold = similarity_threshold
    
    def validate_extraction(
        self,
        openai_result: Dict[str, Any],
        langextract_result: Dict[str, Any],
        regex_result: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Cross-validate and merge extraction results.
        
        Args:
            openai_result: Result from OpenAI direct extraction
            langextract_result: Result from LangExtract with citations
            regex_result: Optional regex fallback results
        
        Returns:
            {
                "merged_result": {...},
                "validation_report": {
                    "agreements": [...],
                    "conflicts": [...],
                    "confidence_scores": {...}
                },
                "audit_trail": {
                    "field_name": {
                        "final_value": "...",
                        "source": "openai|langextract|regex",
                        "openai_value": "...",
                        "langextract_value": "...",
                        "regex_value": "...",
                        "citation": {...},
                        "confidence": 0.95,
                        "needs_review": False
                    }
                }
            }
        """
        merged_permit_details = {}
        report = {
            "agreements": [],
            "conflicts": [],
            "confidence_scores": {}
        }
        audit_trail = {}
        
        # Validate permit details
        permit_fields = [
            'permitNumber', 'permitIssuanceDate', 'facilityName',
            'facilityAddress', 'facilityCounty', 'facilityCity',
            'permitteeName', 'permitteeAddress'
        ]
        
        for field in permit_fields:
            # Get values from each source
            openai_val = openai_result.get('permitDetails', {}).get(field)
            langextract_val = langextract_result.get('permitDetails', {}).get(field)
            regex_val = regex_result.get(field) if regex_result else None
            
            # Get citation from LangExtract
            citation = langextract_result.get('_citations', {}).get(field, {})
            
            # Compare and resolve
            final_value, source, confidence, needs_review = self._resolve_field(
                field=field,
                openai_val=openai_val,
                langextract_val=langextract_val,
                regex_val=regex_val,
                citation=citation
            )
            
            # Track agreements and conflicts
            if openai_val and langextract_val:
                agreement, sim = self._compare_values(openai_val, langextract_val)
                if agreement:
                    report["agreements"].append(field)
                else:
                    report["conflicts"].append({
                        "field": field,
                        "openai": openai_val,
                        "langextract": langextract_val,
                        "regex": regex_val,
                        "resolution": source,
                        "confidence": confidence
                    })
            
            # Build audit trail
            audit_trail[field] = {
                "final_value": final_value,
                "source": source,
                "openai_value": openai_val,
                "langextract_value": langextract_val,
                "regex_value": regex_val,
                "citation": citation,
                "confidence": confidence,
                "needs_review": needs_review
            }
            
            merged_permit_details[field] = final_value
            report["confidence_scores"][field] = confidence
        
        # Validate generators (more complex)
        merged_generators, generator_audit = self._validate_generators(
            openai_result.get('generatorSets', []),
            langextract_result.get('generatorSets', [])
        )
        
        return {
            "merged_result": {
                "permitDetails": merged_permit_details,
                "generatorSets": merged_generators
            },
            "validation_report": report,
            "audit_trail": audit_trail,
            "generator_audit_trail": generator_audit,
            "timestamp": datetime.now().isoformat()
        }
    
    def _compare_values(self, val1: Any, val2: Any) -> Tuple[bool, float]:
        """
        Compare two values using fuzzy matching.
        
        Returns:
            (is_match, similarity_score)
        """
        # Both None
        if val1 is None and val2 is None:
            return True, 0.5
        
        # One None
        if val1 is None or val2 is None:
            return False, 0.0
        
        # Normalize strings
        if isinstance(val1, str) and isinstance(val2, str):
            val1_norm = val1.strip().lower()
            val2_norm = val2.strip().lower()
            
            # Exact match
            if val1_norm == val2_norm:
                return True, 1.0
            
            # Fuzzy match
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
    
    def _resolve_field(
        self,
        field: str,
        openai_val: Any,
        langextract_val: Any,
        regex_val: Any,
        citation: Dict[str, Any]
    ) -> Tuple[Any, str, float, bool]:
        """
        Resolve conflicts using priority rules.
        
        Returns:
            (final_value, source, confidence, needs_review)
        """
        # Check if values agree
        openai_langextract_agree = False
        if openai_val and langextract_val:
            openai_langextract_agree, similarity = self._compare_values(
                openai_val, langextract_val
            )
        
        # CASE 1: All three agree (highest confidence)
        if openai_langextract_agree and regex_val:
            regex_agree, _ = self._compare_values(openai_val, regex_val)
            if regex_agree:
                return openai_val, "all_sources_agree", 1.0, False
        
        # CASE 2: OpenAI and LangExtract agree (high confidence)
        if openai_langextract_agree and citation:
            return openai_val, "openai_langextract_agree", 0.95, False
        
        # CASE 3: LangExtract has citation (prefer traceable)
        if citation and langextract_val and langextract_val != "Not specified":
            # Check if OpenAI significantly disagrees
            if openai_val and openai_val != "Not specified":
                agree, sim = self._compare_values(openai_val, langextract_val)
                if not agree and sim < 0.5:
                    # Significant disagreement - flag for review
                    return langextract_val, "langextract_cited", 0.75, True
            return langextract_val, "langextract_cited", 0.85, False
        
        # CASE 4: OpenAI and regex agree (medium-high confidence)
        if openai_val and regex_val:
            agree, _ = self._compare_values(openai_val, regex_val)
            if agree:
                return openai_val, "openai_regex_agree", 0.80, False
        
        # CASE 5: Only OpenAI has value (medium confidence, flag for review)
        if openai_val and openai_val != "Not specified":
            return openai_val, "openai_only", 0.65, True
        
        # CASE 6: Only LangExtract has value (medium confidence)
        if langextract_val and langextract_val != "Not specified":
            return langextract_val, "langextract_only", 0.60, True
        
        # CASE 7: Only regex has value (low confidence, flag for review)
        if regex_val:
            return regex_val, "regex_only", 0.50, True
        
        # CASE 8: No value found (very low confidence)
        return "Not specified", "no_extraction", 0.20, True
    
    def _validate_generators(
        self,
        openai_gens: List[Dict],
        langextract_gens: List[Dict]
    ) -> Tuple[List[Dict], Dict]:
        """
        Validate and merge generator arrays.
        
        Strategy:
        1. Match generators by reference number
        2. For unmatched, try matching by capacity
        3. Merge matched generators field-by-field
        4. Include unmatched generators with low confidence
        """
        merged = []
        audit = {}
        
        # Create lookup by reference number
        openai_by_ref = {
            g.get('referenceNumber'): g 
            for g in openai_gens 
            if g.get('referenceNumber')
        }
        langextract_by_ref = {
            g.get('referenceNumber'): g 
            for g in langextract_gens 
            if g.get('referenceNumber')
        }
        
        # Match by reference number
        all_refs = set(openai_by_ref.keys()) | set(langextract_by_ref.keys())
        
        for ref in all_refs:
            openai_gen = openai_by_ref.get(ref)
            langextract_gen = langextract_by_ref.get(ref)
            
            if openai_gen and langextract_gen:
                # Both sources have this generator
                merged_gen, gen_audit = self._merge_generator(
                    openai_gen, langextract_gen
                )
                merged.append(merged_gen)
                audit[ref] = gen_audit
            elif openai_gen:
                # Only OpenAI has this
                openai_gen['_confidence'] = 0.60
                openai_gen['_source'] = 'openai_only'
                merged.append(openai_gen)
                audit[ref] = {"source": "openai_only", "needs_review": True}
            else:
                # Only LangExtract has this
                langextract_gen['_confidence'] = 0.70  # Higher if traceable
                langextract_gen['_source'] = 'langextract_only'
                merged.append(langextract_gen)
                audit[ref] = {"source": "langextract_only", "needs_review": False}
        
        return merged, audit
    
    def _merge_generator(
        self,
        openai_gen: Dict[str, Any],
        langextract_gen: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Merge individual generator objects field-by-field.
        
        Returns:
            (merged_generator, audit_info)
        """
        merged = {}
        audit = {"field_sources": {}, "conflicts": []}
        
        # All generator fields
        gen_fields = [
            'referenceNumber', 'numGenerators', 'make', 'model', 'fuelType',
            'ratedCapacityKW', 'ratedCapacityBHP', 'maximumCapacityKW', 
            'maximumCapacityBHP', 'fuelThroughputLimit', 'fuelSulfurContent',
            'controlTechnology', 'operatingHoursLimit',
            'noxEmissionLimitLbsHr', 'noxEmissionLimitTonsYr',
            'coEmissionLimitLbsHr', 'coEmissionLimitTonsYr',
            'vocEmissionLimitLbsHr', 'vocEmissionLimitTonsYr',
            'so2EmissionLimitLbsHr', 'so2EmissionLimitTonsYr',
            'pmEmissionLimitLbsHr', 'pmEmissionLimitTonsYr',
            'pm10EmissionLimitLbsHr', 'pm10EmissionLimitTonsYr',
            'stackTestRequired'
        ]
        
        for field in gen_fields:
            openai_val = openai_gen.get(field)
            langextract_val = langextract_gen.get(field)
            
            # Use simple resolution logic
            if openai_val and langextract_val:
                agree, _ = self._compare_values(openai_val, langextract_val)
                if agree:
                    merged[field] = openai_val
                    audit["field_sources"][field] = "both_agree"
                else:
                    # Prefer LangExtract for emissions (more reliable)
                    if "Emission" in field:
                        merged[field] = langextract_val
                        audit["field_sources"][field] = "langextract_preferred"
                    else:
                        merged[field] = openai_val
                        audit["field_sources"][field] = "openai_preferred"
                    
                    audit["conflicts"].append({
                        "field": field,
                        "openai": openai_val,
                        "langextract": langextract_val
                    })
            elif openai_val:
                merged[field] = openai_val
                audit["field_sources"][field] = "openai_only"
            elif langextract_val:
                merged[field] = langextract_val
                audit["field_sources"][field] = "langextract_only"
            else:
                merged[field] = None
        
        # Add metadata
        merged['_confidence'] = 0.90 if not audit["conflicts"] else 0.75
        merged['_source'] = 'both_sources'
        
        return merged, audit


class ConfidenceScorer:
    """Calculate confidence scores for extracted values."""
    
    @staticmethod
    def score_field(
        field_name: str,
        final_value: Any,
        openai_value: Any,
        langextract_value: Any,
        regex_value: Any,
        has_citation: bool
    ) -> float:
        """
        Calculate confidence score (0-1) for a field.
        
        Factors:
        - Agreement between sources (40%)
        - Citation availability (25%)
        - Regex validation (20%)
        - Value completeness (10%)
        - Field-specific rules (5%)
        """
        score = 0.0
        
        # Factor 1: Source agreement (40%)
        num_sources = sum([
            1 for val in [openai_value, langextract_value, regex_value]
            if val and val != "Not specified"
        ])
        
        if num_sources >= 3:
            score += 0.40
        elif num_sources == 2:
            # Check if they agree
            vals = [v for v in [openai_value, langextract_value, regex_value] 
                   if v and v != "Not specified"]
            if len(vals) == 2:
                from difflib import SequenceMatcher
                if isinstance(vals[0], str) and isinstance(vals[1], str):
                    similarity = SequenceMatcher(None, 
                        str(vals[0]).lower(), 
                        str(vals[1]).lower()
                    ).ratio()
                    score += 0.30 * similarity
                else:
                    score += 0.30
        elif num_sources == 1:
            score += 0.15
        
        # Factor 2: Citation (25%)
        if has_citation:
            score += 0.25
        
        # Factor 3: Regex validation (20%)
        if regex_value and regex_value != "Not specified":
            score += 0.20
        
        # Factor 4: Value completeness (10%)
        if final_value and str(final_value).strip() and final_value != "Not specified":
            value_len = len(str(final_value))
            if value_len > 10:
                score += 0.10
            elif value_len > 5:
                score += 0.07
            else:
                score += 0.04
        
        # Factor 5: Field-specific validation (5%)
        score += ConfidenceScorer._validate_field_format(field_name, final_value)
        
        return min(score, 1.0)
    
    @staticmethod
    def _validate_field_format(field_name: str, value: Any) -> float:
        """Apply field-specific format validation."""
        if not value or value == "Not specified":
            return 0.0
        
        value_str = str(value)
        
        # Permit number: numeric, 4+ digits
        if field_name == "permitNumber":
            if value_str.replace('-', '').isdigit() and len(value_str) >= 4:
                return 0.05
        
        # Date: YYYY-MM-DD format
        if "Date" in field_name:
            import re
            if re.match(r'^\d{4}-\d{2}-\d{2}$', value_str):
                return 0.05
        
        # County: contains "County"
        if field_name == "facilityCounty":
            if "county" in value_str.lower():
                return 0.05
        
        # Emissions: numeric
        if "Emission" in field_name and "Limit" in field_name:
            try:
                float(value_str)
                return 0.05
            except (ValueError, TypeError):
                return 0.0
        
        return 0.02


# Example usage demonstration
def example_usage():
    """Demonstrate the enhanced hybrid validation."""
    
    # Simulated OpenAI result
    openai_result = {
        "permitDetails": {
            "permitNumber": "11541",
            "permitIssuanceDate": "2008-05-16",
            "facilityName": "Southwest Enterprise Solutions Center",
            "facilityAddress": "123 Industrial Drive",
            "facilityCounty": "Russell County",
            "facilityCity": "Lebanon",
            "permitteeName": "Southwest Enterprise LLC",
            "permitteeAddress": "456 Business Rd, Lebanon, VA"
        },
        "generatorSets": [
            {
                "referenceNumber": "1",
                "make": "Caterpillar",
                "model": "3516",
                "ratedCapacityKW": 1500.0,
                "ratedCapacityBHP": 2500.0,
                "noxEmissionLimitLbsHr": 63.90,
                "noxEmissionLimitTonsYr": 15.96
            }
        ]
    }
    
    # Simulated LangExtract result with citations
    langextract_result = {
        "permitDetails": {
            "permitNumber": "11541",
            "permitIssuanceDate": "2008-05-16",
            "facilityName": "Southwest Enterprise Solutions Center",
            "facilityAddress": None,  # Missed this
            "facilityCounty": "Russell",  # Different format
            "facilityCity": "Lebanon",
            "permitteeName": "Southwest Enterprise",  # Slightly different
            "permitteeAddress": "456 Business Road, Lebanon, VA"
        },
        "_citations": {
            "permitNumber": {
                "extraction_text": "Registration No. 11541",
                "char_start": 45,
                "char_end": 68
            },
            "facilityName": {
                "extraction_text": "Southwest Enterprise Solutions Center",
                "char_start": 234,
                "char_end": 271
            }
            # ... other citations
        },
        "generatorSets": [
            {
                "referenceNumber": "1",
                "make": "Caterpillar",
                "model": None,  # Missed model
                "ratedCapacityKW": 1500.0,
                "ratedCapacityBHP": 2500.0,
                "noxEmissionLimitLbsHr": 63.90,  # Agrees
                "noxEmissionLimitTonsYr": 16.0   # Slightly different
            }
        ]
    }
    
    # Simulated regex result
    regex_result = {
        "permitNumber": "11541",
        "facilityCounty": "Russell County"
    }
    
    # Validate
    validator = ExtractionValidator()
    validated = validator.validate_extraction(
        openai_result,
        langextract_result,
        regex_result
    )
    
    # Print results
    print("=" * 80)
    print("VALIDATION REPORT")
    print("=" * 80)
    print(f"\n✅ Agreements: {len(validated['validation_report']['agreements'])}")
    for field in validated['validation_report']['agreements']:
        print(f"   - {field}")
    
    print(f"\n⚠️  Conflicts: {len(validated['validation_report']['conflicts'])}")
    for conflict in validated['validation_report']['conflicts']:
        print(f"   - {conflict['field']}:")
        print(f"     OpenAI: {conflict['openai']}")
        print(f"     LangExtract: {conflict['langextract']}")
        print(f"     Resolution: {conflict['resolution']} (confidence: {conflict['confidence']:.2f})")
    
    print("\n" + "=" * 80)
    print("AUDIT TRAIL SAMPLE")
    print("=" * 80)
    
    for field, audit in list(validated['audit_trail'].items())[:3]:
        print(f"\nField: {field}")
        print(f"  Final Value: {audit['final_value']}")
        print(f"  Source: {audit['source']}")
        print(f"  Confidence: {audit['confidence']:.2f}")
        print(f"  Needs Review: {audit['needs_review']}")
        if audit['citation']:
            print(f"  Citation: \"{audit['citation'].get('extraction_text', 'N/A')}\"")
    
    # Save full results
    output_path = Path("example_validation_output.json")
    with open(output_path, 'w') as f:
        json.dump(validated, f, indent=2)
    
    print(f"\n✅ Full validation results saved to: {output_path}")


if __name__ == "__main__":
    example_usage()
