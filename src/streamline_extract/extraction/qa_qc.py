"""
Universal QA/QC validation layer using LangExtract for extraction accuracy.

This module provides cross-validation between OpenAI structured extraction
and LangExtract targeted extraction, with confidence scoring and automated
error detection for critical fields.

Fully domain-agnostic - works with any document type and schema.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FieldValidation:
    """Validation result for a single field."""
    field_path: str
    openai_value: Any
    langextract_value: Optional[Any]
    confidence: float
    status: str  # 'validated', 'overridden', 'flagged', 'error'
    source_citation: Optional[str]
    reason: Optional[str] = None


@dataclass
class ValidationReport:
    """Complete validation report for a document extraction."""
    entity_identifier: str  # Universal identifier (e.g., document ID, jurisdiction, tariff ID, entity name)
    overall_confidence: float
    total_fields: int
    fields_validated: int
    discrepancies: int
    overrides_applied: int
    flags_for_review: int
    field_validations: List[FieldValidation]
    recommendations: List[str]


class QAQCValidator:
    """
    Universal cross-validation engine for document extraction QA/QC.
    
    Compares OpenAI and LangExtract results, applies confidence-based
    overrides, and generates traceability reports.
    
    Fully schema-agnostic - dynamically detects fields and entities from any schema.
    """
    
    # Thresholds
    OVERRIDE_CONFIDENCE_THRESHOLD = 0.80  # Auto-override if above this
    FLAG_CONFIDENCE_THRESHOLD = 0.60      # Flag for review if below this
    NUMERIC_TOLERANCE = 0.05              # 5% tolerance for numeric matches
    
    def __init__(self, enable_overrides: bool = True):
        """
        Initialize QA/QC validator.
        
        Args:
            enable_overrides: If True, allow automatic overrides based on confidence
        """
        self.enable_overrides = enable_overrides
    
    def validate(
        self,
        openai_data: Dict[str, Any],
        langextract_result: Any,
        entity_identifier: str
    ) -> Tuple[Dict[str, Any], ValidationReport]:
        """
        Cross-validate extraction results and generate report.
        
        Args:
            openai_data: Data from OpenAI structured extraction
            langextract_result: Raw LangExtract extraction result
            entity_identifier: Entity identifier (permit number, jurisdiction, tariff ID, etc.)
            
        Returns:
            Tuple of (validated_data, validation_report)
            - validated_data: OpenAI data with overrides applied
            - validation_report: Detailed validation report
        """
        logger.info(f"🔍 Starting QA/QC validation for {entity_identifier}")
        
        # Parse LangExtract results into structured format
        langextract_data = self._parse_langextract_result(langextract_result)
        
        # Validate fields dynamically based on schema
        field_validations = []
        validated_data = self._deep_copy(openai_data)
        
        # Find main array fields (generatorSets, requirements, rates, etc.)
        for key, value in validated_data.items():
            if isinstance(value, list) and value:
                # This is a main array - validate each item
                le_items = langextract_data.get('items', {})
                
                for idx, item in enumerate(value):
                    if not isinstance(item, dict):
                        continue
                        
                    # Get item identifier
                    item_id = item.get('referenceNumber', item.get('id', item.get('feature', f'item_{idx}')))
                    
                    # Find matching LangExtract data for this item
                    le_item = le_items.get(str(item_id), {})
                    
                    # Validate all numeric and important string fields
                    for field, field_value in item.items():
                        if field_value is not None and (isinstance(field_value, (int, float, str))):
                            validation = self._validate_field(
                                field_path=f'{key}[{idx}].{field}',
                                openai_value=field_value,
                                langextract_value=le_item.get(field),
                                source_citation=le_item.get(f'{field}_citation')
                            )
                            field_validations.append(validation)
                            
                            # Apply override if confidence is high enough
                            if (self.enable_overrides and 
                                validation.status == 'overridden' and
                                validation.langextract_value is not None):
                                item[field] = validation.langextract_value
                                logger.info(f"  ✓ Override applied: {field} = {validation.langextract_value}")
        
        # Calculate overall confidence
        overall_confidence = self._calculate_overall_confidence(field_validations)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(field_validations, overall_confidence)
        
        # Build report
        report = ValidationReport(
            entity_identifier=entity_identifier,
            overall_confidence=overall_confidence,
            total_fields=len(field_validations),
            fields_validated=sum(1 for v in field_validations if v.status == 'validated'),
            discrepancies=sum(1 for v in field_validations if v.status in ['flagged', 'overridden']),
            overrides_applied=sum(1 for v in field_validations if v.status == 'overridden'),
            flags_for_review=sum(1 for v in field_validations if v.status == 'flagged'),
            field_validations=field_validations,
            recommendations=recommendations
        )
        
        logger.info(f"  ✓ QA/QC complete: {report.overall_confidence:.2f} confidence, "
                   f"{report.overrides_applied} overrides, {report.flags_for_review} flags")
        
        return validated_data, report
    
    def _validate_field(
        self,
        field_path: str,
        openai_value: Any,
        langextract_value: Optional[Any],
        source_citation: Optional[str]
    ) -> FieldValidation:
        """
        Validate a single field by comparing OpenAI and LangExtract values.
        
        Returns FieldValidation with confidence score and status.
        """
        # If LangExtract didn't find this field, trust OpenAI with lower confidence
        if langextract_value is None:
            return FieldValidation(
                field_path=field_path,
                openai_value=openai_value,
                langextract_value=None,
                confidence=0.65,  # Medium-low confidence
                status='validated',
                source_citation=None,
                reason='No LangExtract validation available'
            )
        
        # Compare values
        values_match, difference = self._compare_values(openai_value, langextract_value)
        
        # Calculate confidence
        confidence = self._calculate_field_confidence(
            values_match=values_match,
            has_source=source_citation is not None,
            openai_value=openai_value,
            langextract_value=langextract_value
        )
        
        # Determine status
        if values_match:
            status = 'validated'
            reason = f'Values match (±{difference:.1%} difference)'
        elif confidence >= self.OVERRIDE_CONFIDENCE_THRESHOLD:
            status = 'overridden'
            reason = f'High-confidence override (OpenAI: {openai_value}, LangExtract: {langextract_value})'
        elif confidence <= self.FLAG_CONFIDENCE_THRESHOLD:
            status = 'flagged'
            reason = f'Low confidence - manual review needed (OpenAI: {openai_value}, LangExtract: {langextract_value})'
        else:
            status = 'flagged'
            reason = f'Values differ: OpenAI={openai_value}, LangExtract={langextract_value}'
        
        return FieldValidation(
            field_path=field_path,
            openai_value=openai_value,
            langextract_value=langextract_value,
            confidence=confidence,
            status=status,
            source_citation=source_citation,
            reason=reason
        )
    
    def _compare_values(self, val1: Any, val2: Any) -> Tuple[bool, float]:
        """
        Compare two values with tolerance for numeric values.
        
        Returns (match, relative_difference)
        """
        # Handle None cases
        if val1 is None and val2 is None:
            return True, 0.0
        if val1 is None or val2 is None:
            return False, 1.0
        
        # Numeric comparison with tolerance
        if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
            if val1 == 0 and val2 == 0:
                return True, 0.0
            
            avg = (abs(val1) + abs(val2)) / 2
            if avg == 0:
                return val1 == val2, 0.0
            
            diff = abs(val1 - val2) / avg
            return diff <= self.NUMERIC_TOLERANCE, diff
        if isinstance(val1, str) and isinstance(val2, str):
            
            # Fall back to exact match
            match = val1.lower().strip() == val2.lower().strip()
            return match, 0.0 if match else 1.0
        
        # Exact comparison for other types
        return val1 == val2, 0.0 if val1 == val2 else 1.0
    
    def _calculate_field_confidence(
        self,
        values_match: bool,
        has_source: bool,
        openai_value: Any,
        langextract_value: Any
    ) -> float:
        """
        Calculate confidence score for a field (0.0 to 1.0).
        
        Factors:
        - Values match: 0.4
        - Has source citation: 0.3
        - Non-null values: 0.2
        - Reasonable value: 0.1
        """
        confidence = 0.0
        
        # Values match
        if values_match:
            confidence += 0.4
        
        # Has source citation
        if has_source:
            confidence += 0.3
        
        # Both values are non-null
        if openai_value is not None and langextract_value is not None:
            confidence += 0.2
        
        # Value passes basic sanity check
        if self._is_reasonable_value(openai_value):
            confidence += 0.1
        
        return min(confidence, 1.0)
    
    def _is_reasonable_value(self, value: Any) -> bool:
        """Check if a value is reasonable (not obviously wrong)."""
        if value is None:
            return True  # Null is acceptable
        
        if isinstance(value, (int, float)):
            # Check for unrealistic numeric values
            if value < 0:
                return False  # Negative emissions/hours don't make sense
            if value > 1_000_000:
                return False  # Unrealistically large
        
        return True
    
    def _validate_context_fields(
        self, openai_details: dict[str, Any], langextract_details: dict[str, Any]
    ) -> list[FieldValidation]:
        """
        Validate context/metadata fields dynamically.
        
        Detects and validates top-level scalar fields (strings, numbers)
        which are typically identifier/metadata fields.
        """
        validations = []
        
        # Dynamically detect context fields (non-list, non-dict values at top level)
        for field, value in openai_details.items():
            if isinstance(value, (str, int, float, bool)) and value is not None:
                validation = self._validate_field(
                    field_path=field,
                    openai_value=value,
                    langextract_value=langextract_details.get(field),
                    source_citation=langextract_details.get(f'{field}_citation')
                )
                validations.append(validation)
        
        return validations
    
    def _calculate_overall_confidence(self, validations: List[FieldValidation]) -> float:
        """Calculate overall extraction confidence from field validations."""
        if not validations:
            return 0.5
        
        # Weighted average (critical fields count more)
        total_weight = 0.0
        weighted_sum = 0.0
        
        for validation in validations:
            # Critical fields get 2x weight
            weight = 2.0 if self._is_critical_field(validation.field_path) else 1.0
            weighted_sum += validation.confidence * weight
            total_weight += weight
        
        return weighted_sum / total_weight if total_weight > 0 else 0.5
    
    def _is_critical_field(self, field_path: str) -> bool:
        """Check if a field path refers to a critical field."""
        return any(critical in field_path for critical in self.CRITICAL_FIELDS)
    
    def _generate_recommendations(
        self,
        validations: List[FieldValidation],
        overall_confidence: float
    ) -> List[str]:
        """Generate actionable recommendations based on validation results."""
        recommendations = []
        
        # Overall confidence check
        if overall_confidence < 0.70:
            recommendations.append(
                f"⚠️  Low overall confidence ({overall_confidence:.2f}) - manual review strongly recommended"
            )
        elif overall_confidence < 0.85:
            recommendations.append(
                f"Moderate confidence ({overall_confidence:.2f}) - spot-check flagged fields"
            )
        
        # Flagged fields
        flagged = [v for v in validations if v.status == 'flagged']
        if flagged:
            recommendations.append(
                f"Review {len(flagged)} flagged field(s): " + 
                ", ".join([v.field_path for v in flagged[:3]]) +
                (f" and {len(flagged)-3} more" if len(flagged) > 3 else "")
            )
        
        # Overrides applied
        overrides = [v for v in validations if v.status == 'overridden']
        if overrides:
            recommendations.append(
                f"✓ {len(overrides)} automatic override(s) applied based on high-confidence LangExtract data"
            )
        
        # Missing citations
        no_citation = [v for v in validations if v.source_citation is None and self._is_critical_field(v.field_path)]
        if no_citation:
            recommendations.append(
                f"⚠️  {len(no_citation)} critical field(s) lack source citations"
            )
        
        if not recommendations:
            recommendations.append("✓ All validations passed - extraction appears accurate")
        
        return recommendations
    
    def _parse_langextract_result(self, result: Any) -> Dict[str, Any]:
        """
        Parse LangExtract extraction result into structured format.
        
        Organizes extractions by item ID and creates a generic structure.
        """
        if not result or not hasattr(result, 'extractions'):
            return {'items': {}}
        
        items = {}
        
        for extraction in result.extractions:
            attrs = extraction.attributes if extraction.attributes else {}
            extraction_text = extraction.extraction_text
            
            # Get item identifier (could be generator_id, feature, requirement_id, etc.)
            item_id = None
            for id_field in ['generator_id', 'item_id', 'id', 'feature', 'requirement_id']:
                if id_field in attrs:
                    item_id = attrs[id_field]
                    break
            
            if not item_id:
                continue  # Skip extractions without an identifier
            
            if item_id not in items:
                items[item_id] = {}
            
            # Store all attributes from the extraction generically
            for key, value in attrs.items():
                if key not in ['generator_id', 'item_id', 'id']:  # Don't duplicate IDs
                    # Store the value
                    items[item_id][key] = value
                    # Store citation
                    items[item_id][f'{key}_citation'] = extraction_text
        
        return {'items': items}
    
    def _deep_copy(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a deep copy of data dictionary."""
        import json
        return json.loads(json.dumps(data))
