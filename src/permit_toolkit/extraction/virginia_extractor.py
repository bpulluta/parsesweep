"""Virginia-specific permit extractor."""

import re
import logging
from pathlib import Path
from typing import Dict, Any, List

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False
    lx = None

from permit_toolkit.extraction.base_extractor import BasePermitExtractor

logger = logging.getLogger(__name__)


class VirginiaPermitExtractor(BasePermitExtractor):
    """
    Extract structured data from Virginia DEQ air quality permits.
    
    Virginia-specific patterns:
    - "Registration No" (not "Permit No")
    - "is authorized to" authorization section
    - "Location:" field in header
    - DEQ letterhead structure: "COMMONWEALTH of VIRGINIA / DEPARTMENT OF ENVIRONMENTAL QUALITY"
    
    Uses hybrid approach:
    1. LLM extraction for complex fields (generator specs, emissions)
    2. Regex fallback for structured fields (facility name, address, county)
    """
    
    STATE_NAME = "virginia"
    
    def _create_state_examples(self) -> List:
        """
        Virginia-specific examples for GENERATOR extraction only.
        
        OpenAI handles permit details (facility name, address, county, etc).
        LangExtract focuses on generators, specs, and emissions with traceability.
        
        Uses ACTUAL Virginia DEQ permit format from ground truth PDFs (tabular equipment list).
        """
        return [
            # Example 1: Range notation with quantity - CRITICAL for 11790
            lx.data.ExampleData(
                text="""Equipment to be constructed:
Ref. No. Equipment Description Rated Capacity Original Permit Date:
EG01-EG06 (6) Cummins QSK78-G12 diesel-fueled engine-generator sets 2500 kW 4060 hp (nameplate) each 8/23/2016
EG07 Cummins QSK19-G8 diesel-fueled engine-generator set 600 kW 967 hp (nameplate) 8/23/2016

EMISSION LIMITATIONS
Emissions from each emergency diesel-fueled engine-generator set (EG01-EG07) shall not exceed:
NOx: 53.7 lbs/hr per unit (EG01-EG06), 12.79 lbs/hr (EG07)
CO: 3.85 lbs/hr per unit (EG01-EG06), 1.08 lbs/hr (EG07)""",
                extractions=[
                    # Range notation: EG01-EG06 represents 6 generators
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="(6) Cummins QSK78-G12 diesel-fueled engine-generator sets",
                        attributes={"generator_id": "EG01-EG06", "make": "Cummins", "model": "QSK78-G12"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2500 kW",
                        attributes={"generator_id": "EG01-EG06", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="4060 hp (nameplate)",
                        attributes={"generator_id": "EG01-EG06", "type": "capacity_bhp"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="(6)",
                        attributes={"generator_id": "EG01-EG06", "type": "quantity"}
                    ),
                    # Single generator: EG07
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="Cummins QSK19-G8 diesel-fueled engine-generator set",
                        attributes={"generator_id": "EG07", "make": "Cummins", "model": "QSK19-G8"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="600 kW",
                        attributes={"generator_id": "EG07", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="967 hp (nameplate)",
                        attributes={"generator_id": "EG07", "type": "capacity_bhp"}
                    ),
                    # Emissions apply to all via propagation
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="NOx: 53.7 lbs/hr",
                        attributes={"generator_id": "EG01-EG06", "pollutant": "nox"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="NOx: 12.79 lbs/hr",
                        attributes={"generator_id": "EG07", "pollutant": "nox"}
                    ),
                ],
            ),
            # Example 2: Single generator with inline format
            lx.data.ExampleData(
                text="""EQUIPMENT
Ref No. 1: One Caterpillar 1500 KW diesel powered emergency generator, 2500 brake horsepower

The distillate oil consumed by the generators shall meet ASTM D396-78 specifications for numbers 1 or 2 fuel oil, with a maximum sulfur content of 0.5 weight percent.

The Caterpillar diesel powered emergency generators shall not operate more than 500 hours per year each, calculated monthly as the sum of each consecutive 12-month period.

EMISSION LIMITATIONS
Emission Limits-Emissions from each Caterpillar diesel powered emergency generator exhaust stack shall not exceed the limits specified below:
Sulfur Dioxide: 4.20 lbs/hr, 1.05 tons/yr
Nitrogen Oxides(as NO2): 63.90 lbs/hr, 15.96 tons/yr
Carbon Monoxide: 13.77 lbs/hr, 3.44 tons/yr
Particulate Matter(PM-10): 4.49 lbs/hr, 1.12 tons/yr
Volatile Organic Compounds: 5.07 lbs/hr, 1.27 tons/yr""",
                extractions=[
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Caterpillar 1500 KW diesel powered emergency generator",
                        attributes={"generator_id": "1", "make": "Caterpillar"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="1500 KW",
                        attributes={"generator_id": "1", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2500 brake horsepower",
                        attributes={"generator_id": "1", "type": "capacity_bhp"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="distillate oil",
                        attributes={"generator_id": "1", "type": "fuel"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="0.5 weight percent",
                        attributes={"generator_id": "1", "type": "sulfur"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="500 hours per year",
                        attributes={"generator_id": "1", "type": "hours"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Nitrogen Oxides(as NO2): 63.90 lbs/hr, 15.96 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "nox"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Carbon Monoxide: 13.77 lbs/hr, 3.44 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "co"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Volatile Organic Compounds: 5.07 lbs/hr, 1.27 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "voc"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Sulfur Dioxide: 4.20 lbs/hr, 1.05 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "so2"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Particulate Matter(PM-10): 4.49 lbs/hr, 1.12 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "pm10"}
                    ),
                ],
            ),
            # Example 3: Multiple generators in tabular format (matching 11541)
            lx.data.ExampleData(
                text="""Equipment List - Equipment at this facility consists of the following:
Ref No. Equipment Description Rated Capacity
3 One Caterpillar 1500 KW diesel powered emergency generator 2500 brake horsepower
2 One Caterpillar 1500 KW diesel powered emergency generator 2500 brake horsepower  
1 One Caterpillar 1500 KW diesel powered emergency generator 2500 brake horsepower

OPERATING LIMITATIONS
The Caterpillar diesel powered emergency generators shall not operate more than 500 hours per year each.
Fuel - The approved fuel is distillate oil with a maximum sulfur content of 0.5 weight percent.

EMISSION LIMITATIONS
Emissions from each generator shall not exceed:
Nitrogen Oxides(as NO2): 63.90 lbs/hr, 15.96 tons/yr
Carbon Monoxide: 13.77 lbs/hr, 3.44 tons/yr""",
                extractions=[
                    # Generator 3
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Caterpillar 1500 KW diesel powered emergency generator",
                        attributes={"generator_id": "3", "make": "Caterpillar"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="1500 KW",
                        attributes={"generator_id": "3", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2500 brake horsepower",
                        attributes={"generator_id": "3", "type": "capacity_bhp"}
                    ),
                    # Generator 2
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Caterpillar 1500 KW diesel powered emergency generator",
                        attributes={"generator_id": "2", "make": "Caterpillar"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="1500 KW",
                        attributes={"generator_id": "2", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2500 brake horsepower",
                        attributes={"generator_id": "2", "type": "capacity_bhp"}
                    ),
                    # Generator 1
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Caterpillar 1500 KW diesel powered emergency generator",
                        attributes={"generator_id": "1", "make": "Caterpillar"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="1500 KW",
                        attributes={"generator_id": "1", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2500 brake horsepower",
                        attributes={"generator_id": "1", "type": "capacity_bhp"}
                    ),
                    # Shared specs/emissions (apply to all generators via propagation)
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="distillate oil",
                        attributes={"generator_id": "1", "type": "fuel"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="500 hours per year",
                        attributes={"generator_id": "1", "type": "hours"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Nitrogen Oxides(as NO2): 63.90 lbs/hr, 15.96 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "nox"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Carbon Monoxide: 13.77 lbs/hr, 3.44 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "co"}
                    ),
                ],
            ),
        ]
    
    def _extract_permit_details_fallback(self, text: str, pdf_path: Path) -> Dict[str, Any]:
        """
        Regex-based fallback extraction for Virginia DEQ permits.
        
        Virginia-specific markers:
        - "Registration No" (not "Permit No" like Illinois)
        - "is authorized to" authorization section
        - "Location:" field in header
        - Virginia DEQ letterhead structure
        
        Args:
            text: Full PDF text
            pdf_path: Path to PDF (for permit number from filename)
            
        Returns:
            Dict with permitDetails fields (only non-null values)
        """
        details = {}
        
        # 1. PERMIT NUMBER - from filename or Registration No.
        permit_match = re.search(r'Registration No[.:\s]+(\d{4,})', text, re.IGNORECASE)
        if permit_match:
            details['permitNumber'] = permit_match.group(1)
        elif pdf_path:
            filename_match = re.search(r'(\d{4,})', pdf_path.stem)
            if filename_match:
                details['permitNumber'] = filename_match.group(1)
        
        # 2. ISSUE DATE - at top of letter (first 1500 chars)
        date_match = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
            text[:1500],
            re.IGNORECASE
        )
        if date_match:
            month_names = {
                'january': '01', 'february': '02', 'march': '03', 'april': '04',
                'may': '05', 'june': '06', 'july': '07', 'august': '08',
                'september': '09', 'october': '10', 'november': '11', 'december': '12'
            }
            month = month_names[date_match.group(1).lower()]
            day = date_match.group(2).zfill(2)
            year = date_match.group(3)
            details['permitIssuanceDate'] = f"{year}-{month}-{day}"
        
        # 3. FACILITY NAME & ADDRESS - from authorization section
        auth_match = re.search(
            r'is authorized to\s+(?:construct and operate|modify and operate|operate and modify|operate)\s+(.*?)(?=\n\n|Approved|David K\.|Page \d)',
            text,
            re.IGNORECASE | re.DOTALL
        )
        
        if auth_match:
            auth_text = auth_match.group(1)
            
            # Try to find "the [FACILITY NAME]" before "located"
            facility_match = re.search(
                r'the\s+([A-Z][^\n]{10,100}?)\s+(?:located|in\s+[A-Z][a-z]+,)',
                auth_text,
                re.IGNORECASE
            )
            if facility_match:
                facility_name = facility_match.group(1).strip()
                facility_name = re.sub(r'\s+(in|located|at|on)\s*$', '', facility_name, flags=re.IGNORECASE)
                if len(facility_name) > 10 and not any(word in facility_name.lower() for word in ['authorized', 'equipment', 'generator', 'engine']):
                    details['facilityName'] = facility_name
            
            # Extract address after "located at/on"
            address_match = re.search(
                r'located\s+(?:at|on)\s+(.*?)(?=\nin accordance|\nApproved|David K\.|Page \d)',
                auth_text,
                re.IGNORECASE | re.DOTALL
            )
            if address_match:
                address_text = address_match.group(1).strip()
                address_lines = [line.strip() for line in address_text.split('\n') if line.strip()]
                address_parts = []
                for line in address_lines[:3]:
                    if not any(word in line.lower() for word in ['accordance', 'condition', 'approved', 'permit']):
                        address_parts.append(line)
                
                if address_parts:
                    full_address = ', '.join(address_parts)
                    if not details.get('facilityName') and not re.search(r'\d', address_parts[0]):
                        details['facilityName'] = address_parts[0]
                        if len(address_parts) > 1:
                            full_address = ', '.join(address_parts[1:])
                    
                    details['facilityAddress'] = full_address
        
        # 4. COUNTY - from Location field or address
        location_match = re.search(r'Location:\s*([^,\n]+County)', text[:3000], re.IGNORECASE)
        if location_match:
            details['facilityCounty'] = location_match.group(1)
        else:
            county_in_address = re.search(
                r',\s*([A-Z][a-z]+\s+County)(?:,|\s+Virginia|\s+VA)',
                text[:5000],
                re.IGNORECASE
            )
            if county_in_address:
                details['facilityCounty'] = county_in_address.group(1)
        
        return details


# Backward compatibility: alias for existing code
PermitExtractorLangExtract = VirginiaPermitExtractor
