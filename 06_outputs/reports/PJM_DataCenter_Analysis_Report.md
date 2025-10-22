# PJM Territory Data Center Air Quality Permits Analysis

## Executive Summary

Analysis of EPA's ICIS Air Quality Permits database for data centers in the PJM (Pennsylvania-New Jersey-Maryland Interconnection) territory reveals **363 total data center facilities** with air quality permits across 12 states, with **309 currently operating**.

## PJM Territory States Analyzed
- Delaware (DE)
- Illinois (IL) 
- Indiana (IN)
- Kentucky (KY)
- Maryland (MD)
- Michigan (MI)
- New Jersey (NJ)
- North Carolina (NC)
- Ohio (OH)
- Pennsylvania (PA)
- Tennessee (TN)
- Virginia (VA)
- West Virginia (WV)

## Key Findings

### Total Facilities by State
| State | Total | Operating | % Operating |
|-------|-------|-----------|-------------|
| **Virginia** | 163 | 144 | 88% |
| **Illinois** | 69 | 48 | 70% |
| **Pennsylvania** | 27 | 22 | 81% |
| **New Jersey** | 24 | 22 | 92% |
| **Ohio** | 19 | 19 | 100% |
| **Maryland** | 15 | 14 | 93% |
| **North Carolina** | 14 | 10 | 71% |
| **Tennessee** | 10 | 10 | 100% |
| **Indiana** | 8 | 7 | 88% |
| **Delaware** | 7 | 6 | 86% |
| **Michigan** | 5 | 5 | 100% |
| **Kentucky** | 2 | 2 | 100% |

### Operating Status Distribution
- **Operating**: 309 facilities (85%)
- **Permanently Closed**: 40 facilities (11%)
- **Planned Facility**: 2 facilities (1%)

### Air Pollutant Classification
- **Synthetic Minor Emissions**: 194 facilities (53%)
- **Minor Emissions**: 143 facilities (39%)
- **Major Emissions**: 21 facilities (6%)
- **Not applicable**: 1 facility

### Facility Types
- **NON (Non-Point Source)**: 167 facilities (46%)
- **POF (Point Source)**: 125 facilities (34%)
- **COR (Corporate)**: 19 facilities (5%)
- **CNG (Conditionally Exempt Non-Point)**: 1 facility
- **FDF (Federal Facility)**: 1 facility

## Major Data Centers (Major Emissions Class)

The following 20 data centers are classified as "Major Emissions" facilities, indicating higher environmental impact:

### Illinois (4 facilities)
- ZCOLO LLC - Oak Brook
- ENSONO DATA CENTER - Downers Grove  
- T5@CHICAGO II LP - Elk Grove Village
- GOLDFRAME LLC - DeKalb

### Indiana (2 facilities)
- HATCHWORKS LLC - Fort Wayne
- AMAZON DATA SERVICES - New Carlisle (2 facilities)

### North Carolina (1 facility)
- TAPAHA DYNAMICS, LLC - Lenoir

### Ohio (6 facilities)
- MAGELLAN ENTERPRISES LLC - Columbus
- CMH091 CAMPUS - Hilliard
- SIDECAT LLC - Ohio
- MONTAUK INNOVATIONS LLC - Ohio
- CMH070 CAMPUS - Ohio
- CMH086 CAMPUS - Plain City

### Tennessee (1 facility)
- FOXMAN LLC DBA FOXMAN - Clarksville

### Virginia (4 facilities)
- DIGITAL REALTY - Ashburn Campus
- DIGITAL LOUDOUN PKWY CENTER N LLC - Ashburn
- MICROSOFT CORPORATION - Boydton
- QTS RICHMOND DATA CENTER - Sandston

### Michigan (1 facility)
- FCA US LLC - CHRYSLER TECHNOLOGY CENTER - Auburn Hills

## Notable Hyperscale Data Centers

### Virginia (Data Center Capital)
Virginia leads with 163 facilities, particularly concentrated in:
- **Loudoun County (Ashburn)**: Major cloud provider hub
- **Mecklenburg County (Boydton)**: Microsoft data center campus

### Illinois (Midwest Hub)
69 facilities including major operators:
- **DuPage County**: High concentration around Chicago metro
- **Cook County**: Enterprise data centers

### Amazon Web Services (AWS)
- Multiple facilities in Indiana (New Carlisle)
- Major presence in Virginia

### Enterprise Data Centers
- JP Morgan Chase: Multiple facilities in Delaware
- Microsoft: Large campus in Virginia
- Digital Realty: Major provider in Virginia

## Data Sources & Methodology

**Data Source**: EPA's Integrated Compliance Information System (ICIS) for Clean Air Act Stationary Sources

**Identification Criteria**:
- NAICS codes: 518210 (Data Processing/Hosting), 541511-541519 (Computer Services)
- Facility names containing: data center, server farm, hosting, cloud computing, etc.

**Files Generated**:
- `pjm_data_centers_complete.csv` - All 363 facilities
- `pjm_operating_data_centers.csv` - 309 operating facilities only

## Regulatory Implications

1. **Major Emissions Facilities** (21 total) require:
   - Title V operating permits
   - Enhanced monitoring and reporting
   - Stricter emissions limits

2. **Synthetic Minor Facilities** (194 total):
   - Voluntary emissions limitations
   - Reduced regulatory burden
   - Potential for operational restrictions

3. **Minor Emissions Facilities** (143 total):
   - Basic air quality permits
   - Standard compliance requirements

## Conclusions

The PJM territory contains a significant concentration of data center infrastructure with established air quality permitting. Virginia dominates the region with 45% of all facilities, reflecting its position as a major cloud computing hub. The high percentage of operating facilities (85%) indicates a robust and active data center market across the PJM footprint.

The presence of major cloud providers (Microsoft, Amazon) and hyperscale facilities demonstrates the strategic importance of the PJM region for digital infrastructure supporting the East Coast and Midwest markets.