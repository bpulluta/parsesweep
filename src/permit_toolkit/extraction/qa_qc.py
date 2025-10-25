"""
QA/QC validation layer using LangExtract for policy-grade accuracy.

This module provides cross-validation between OpenAI structured extraction
and LangExtract targeted extraction, with confidence scoring and automated
error detection for critical fields.
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
    """Complete validation report for a permit extraction."""
    permit_number: str
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
    Cross-validation engine for permit extraction QA/QC.
    
    Compares OpenAI and LangExtract results, applies confidence-based
    overrides, and generates traceability reports.
    """
    
    # Critical fields that MUST be accurate for policy decisions
    CRITICAL_FIELDS = {
        'noxEmissionLimitLbsHr', 'noxEmissionLimitTonsYr',
        'coEmissionLimitLbsHr', 'coEmissionLimitTonsYr',
        'vocEmissionLimitLbsHr', 'vocEmissionLimitTonsYr',
        'pmEmissionLimitLbsHr', 'pmEmissionLimitTonsYr',
        'pm10EmissionLimitLbsHr', 'pm10EmissionLimitTonsYr',
        'pm25EmissionLimitLbsHr', 'pm25EmissionLimitTonsYr',
        'so2EmissionLimitLbsHr', 'so2EmissionLimitTonsYr',
        'operatingHoursLimit',
        'fuelThroughputLimit',
        'fuelSulfurContent',
        'numGenerators'
    }
    
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
        permit_number: str
    ) -> Tuple[Dict[str, Any], ValidationReport]:
        """
        Cross-validate extraction results and generate report.
        
        Args:
            openai_data: Data from OpenAI structured extraction
            langextract_result: Raw LangExtract extraction result
            permit_number: Permit identifier
            
        Returns:
            Tuple of (validated_data, validation_report)
            - validated_data: OpenAI data with overrides applied
            - validation_report: Detailed validation report
        """
        logger.info(f"🔍 Starting QA/QC validation for permit {permit_number}")
        
        # Parse LangExtract results into structured format
        langextract_data = self._parse_langextract_result(langextract_result)
        
        # Validate each generator set
        field_validations = []
        validated_data = self._deep_copy(openai_data)
        
        generators = validated_data.get('generatorSets', [])
        for idx, generator in enumerate(generators):
            gen_ref = generator.get('referenceNumber', f'gen_{idx}')
            
            # Find matching LangExtract data for this generator
            le_generator = self._find_matching_generator(gen_ref, langextract_data)
            
            # Validate critical fields
            for field in self.CRITICAL_FIELDS:
                if field in generator:
                    validation = self._validate_field(
                        field_path=f'generatorSets[{idx}].{field}',
                        openai_value=generator[field],
                        langextract_value=le_generator.get(field) if le_generator else None,
                        source_citation=le_generator.get(f'{field}_citation') if le_generator else None
                    )
                    field_validations.append(validation)
                    
                    # Apply override if confidence is high enough
                    if (self.enable_overrides and 
                        validation.status == 'overridden' and
                        validation.langextract_value is not None):
                        generator[field] = validation.langextract_value
                        logger.info(f"  ✓ Override applied: {field} = {validation.langextract_value}")
        
        # Validate permit details
        permit_details_validations = self._validate_permit_details(
            openai_data.get('permitDetails', {}),
            langextract_data.get('permitDetails', {})
        )
        field_validations.extend(permit_details_validations)
        
        # Calculate overall confidence
        overall_confidence = self._calculate_overall_confidence(field_validations)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(field_validations, overall_confidence)
        
        # Build report
        report = ValidationReport(
            permit_number=permit_number,
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
        
        # String comparison (case-insensitive)
        if isinstance(val1, str) and isinstance(val2, str):
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
    
    def _validate_permit_details(
        self,
        openai_details: Dict[str, Any],
        langextract_details: Dict[str, Any]
    ) -> List[FieldValidation]:
        """Validate permit detail fields."""
        validations = []
        
        for field in ['permitNumber', 'facilityName', 'facilityCounty']:
            if field in openai_details:
                validation = self._validate_field(
                    field_path=f'permitDetails.{field}',
                    openai_value=openai_details[field],
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
        
        Organizes extractions by generator ID and field type.
        """
        if not result or not hasattr(result, 'extractions'):
            return {'generators': {}, 'permitDetails': {}}
        
        generators = {}
        permit_details = {}
        
        for extraction in result.extractions:
            attrs = extraction.attributes if extraction.attributes else {}
            gen_id = attrs.get('generator_id')
            extraction_text = extraction.extraction_text
            
            if extraction.extraction_class == "EMISSION":
                # Parse emission limits
                self._parse_emission_extraction(gen_id, extraction_text, attrs, generators)
            
            elif extraction.extraction_class == "SPEC":
                # Parse specifications (hours, capacity, etc.)
                self._parse_spec_extraction(gen_id, extraction_text, attrs, generators)
            
            elif extraction.extraction_class == "GENERATOR":
                # Parse generator info (make, model)
                self._parse_generator_extraction(gen_id, extraction_text, attrs, generators)
            
            elif extraction.extraction_class == "FUEL":
                # Parse fuel specifications
                self._parse_fuel_extraction(gen_id, extraction_text, attrs, generators)
            
            elif extraction.extraction_class == "CONTROL":
                # Parse control technology
                self._parse_control_extraction(gen_id, extraction_text, attrs, generators)
        
        return {
            'generators': generators,
            'permitDetails': permit_details
        }
    
    def _parse_emission_extraction(
        self,
        gen_id: Optional[str],
        text: str,
        attrs: Dict[str, Any],
        generators: Dict[str, Dict]
    ):
        """Parse emission limit extraction with improved value extraction."""
        if not gen_id:
            return
        
        if gen_id not in generators:
            generators[gen_id] = {}
        
        pollutant = attrs.get('pollutant', '').lower()
        
        # Try to get values from attributes first (more reliable)
        lbs_hr = attrs.get('lbs_hr')
        tons_yr = attrs.get('tons_yr')
        
        # If not in attributes, extract from text
        if not lbs_hr or not tons_yr:
            import re
            # Look for patterns like "53.7 lbs/hr" or "53.7 tons/yr"
            numbers = re.findall(r'(\d+\.?\d*)\s*(?:lbs?/h|pounds?/h|tons?/y)', text.lower())
            
            if 'lbs/hr' in text.lower() or 'lbs/h' in text.lower():
                if not lbs_hr and numbers:
                    lbs_hr = numbers[0] if isinstance(numbers[0], str) else str(numbers[0])
            
            if 'tons/yr' in text.lower() or 'tons/y' in text.lower():
                tons_numbers = re.findall(r'(\d+\.?\d*)\s*tons?/y', text.lower())
                if not tons_yr and tons_numbers:
                    tons_yr = tons_numbers[0] if isinstance(tons_numbers[0], str) else str(tons_numbers[0])
        
        if pollutant:
            # Store both lbs/hr and tons/yr if available
            if lbs_hr:
                try:
                    generators[gen_id][f'{pollutant}EmissionLimitLbsHr'] = float(lbs_hr)
                    generators[gen_id][f'{pollutant}EmissionLimitLbsHr_citation'] = text
                except (ValueError, TypeError):
                    pass
            
            if tons_yr:
                try:
                    generators[gen_id][f'{pollutant}EmissionLimitTonsYr'] = float(tons_yr)
                    generators[gen_id][f'{pollutant}EmissionLimitTonsYr_citation'] = text
                except (ValueError, TypeError):
                    pass
    
    def _parse_spec_extraction(
        self,
        gen_id: Optional[str],
        text: str,
        attrs: Dict[str, Any],
        generators: Dict[str, Dict]
    ):
        """Parse specification extraction with improved value extraction."""
        if not gen_id:
            return
        
        if gen_id not in generators:
            generators[gen_id] = {}
        
        spec_type = attrs.get('type', '')
        
        # Try to get value from attributes first
        value = attrs.get('value')
        
        # If not in attributes, extract from text
        if not value:
            import re
            numbers = re.findall(r'\d+\.?\d*', text)
            if numbers:
                value = numbers[0]
        
        if value and spec_type:
            try:
                value_float = float(value)
                
                if spec_type == 'hours':
                    generators[gen_id]['operatingHoursLimit'] = value_float
                    generators[gen_id]['operatingHoursLimit_citation'] = text
                
                elif spec_type == 'capacity_kw':
                    generators[gen_id]['ratedCapacityKW'] = value_float
                    generators[gen_id]['ratedCapacityKW_citation'] = text
                
                elif spec_type == 'capacity_bhp':
                    generators[gen_id]['ratedCapacityBHP'] = value_float
                    generators[gen_id]['ratedCapacityBHP_citation'] = text
                
            except (ValueError, TypeError):
                pass
    
    def _parse_generator_extraction(
        self,
        gen_id: Optional[str],
        text: str,
        attrs: Dict[str, Any],
        generators: Dict[str, Dict]
    ):
        """Parse generator information extraction."""
        if not gen_id:
            return
        
        if gen_id not in generators:
            generators[gen_id] = {}
        
        generators[gen_id]['make'] = attrs.get('make')
        generators[gen_id]['model'] = attrs.get('model')
        generators[gen_id]['make_citation'] = text
        
        # Get quantity if available
        quantity = attrs.get('quantity')
        if quantity:
            try:
                generators[gen_id]['numGenerators'] = int(quantity)
            except (ValueError, TypeError):
                pass
    
    def _parse_fuel_extraction(
        self,
        gen_id: Optional[str],
        text: str,
        attrs: Dict[str, Any],
        generators: Dict[str, Dict]
    ):
        """Parse fuel specification extraction."""
        if not gen_id:
            return
        
        if gen_id not in generators:
            generators[gen_id] = {}
        
        fuel_type = attrs.get('type', '')
        value = attrs.get('value')
        
        if fuel_type == 'fuel_type':
            generators[gen_id]['primaryFuelType'] = text
            generators[gen_id]['primaryFuelType_citation'] = text
        
        elif fuel_type == 'sulfur':
            if value:
                try:
                    # Convert percentage or ppm to decimal
                    sulfur_val = float(value)
                    # If value looks like percentage (>0.01), assume it's % and convert
                    if sulfur_val > 0.01:
                        sulfur_val = sulfur_val / 100
                    generators[gen_id]['fuelSulfurContent'] = sulfur_val
                    generators[gen_id]['fuelSulfurContent_citation'] = text
                except (ValueError, TypeError):
                    pass
        
        elif fuel_type == 'throughput':
            if value:
                try:
                    generators[gen_id]['fuelThroughputLimit'] = float(value)
                    generators[gen_id]['fuelThroughputLimit_citation'] = text
                except (ValueError, TypeError):
                    pass
    
    def _parse_control_extraction(
        self,
        gen_id: Optional[str],
        text: str,
        attrs: Dict[str, Any],
        generators: Dict[str, Dict]
    ):
        """Parse control technology extraction."""
        if not gen_id:
            return
        
        if gen_id not in generators:
            generators[gen_id] = {}
        
        generators[gen_id]['controlTechnology'] = text
        generators[gen_id]['controlTechnology_citation'] = text
    
    def _find_matching_generator(
        self,
        gen_ref: str,
        langextract_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Find LangExtract data matching a generator reference number."""
        generators = langextract_data.get('generators', {})
        
        # Direct match
        if gen_ref in generators:
            return generators[gen_ref]
        
        # Handle range notation (e.g., "EG01-EG06" matches "EG01-EG06", "EG01", "EG03", etc.)
        if '-' in gen_ref:
            # This is a range, return data for the range
            if gen_ref in generators:
                return generators[gen_ref]
        else:
            # This might be part of a range, look for matching range
            for le_ref, le_data in generators.items():
                if '-' in le_ref:
                    # Check if gen_ref falls within this range
                    start, end = le_ref.split('-')
                    if self._ref_in_range(gen_ref, start, end):
                        return le_data
        
        return None
    
    def _ref_in_range(self, ref: str, start: str, end: str) -> bool:
        """Check if a reference number falls within a range."""
        # Simple implementation - just check prefix match
        return ref.startswith(start[:2]) if len(start) >= 2 else False
    
    def _deep_copy(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a deep copy of data dictionary."""
        import json
        return json.loads(json.dumps(data))
