# Multi-State Scraper Strategy

## Overview
Based on the successful Virginia DEQ scraper, this document outlines the strategy for scraping air quality permits from all PJM territory states.

## PJM Territory States (14 jurisdictions)
1. ✅ **Virginia** - COMPLETE (188 permits, 100% success rate)
2. Delaware
3. Illinois
4. Indiana
5. Kentucky
6. Maryland
7. Michigan
8. New Jersey
9. North Carolina
10. Ohio
11. Pennsylvania
12. Tennessee
13. West Virginia
14. District of Columbia

## Architecture: Hybrid Approach

### Base Class Pattern
Create a shared `BasePermitScraper` with common functionality:
- Playwright browser management
- PDF download/rendering logic
- Resume capability
- Error handling
- Logging infrastructure

### State-Specific Implementations
Extend base class for each state's unique requirements:
- Custom URL patterns
- State-specific selectors
- Different authentication methods
- Pagination strategies

## Implementation Priority

### Tier 1: High Data Center Concentration
1. **Virginia** ✅ - DONE (177 total permits with 11 duplicates, only pulled the latest)
2. **Maryland** - Frederick County has multiple facilities
3. **Pennsylvania** - Growing data center market
4. **New Jersey** - GP-005 generator permits

### Tier 2: Medium Concentration
5. **Ohio** - Several facilities
6. **North Carolina** - Emerging market
7. **Illinois** - Chicago area

### Tier 3: Lower Priority
8. **Indiana, Kentucky, Michigan, Tennessee, West Virginia, Delaware, DC**

## State-by-State Analysis

### Virginia ✅
- **Status**: Complete
- **Method**: Playwright page.pdf()
- **URL**: https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-air-permits-for-data-centers
- **Permits**: 188 PDFs
- **Challenges**: Anti-bot protection, embedded PDF viewer
- **Success Rate**: 100%

### Maryland
- **Agency**: Maryland Department of the Environment (MDE)
- **Likely URL**: https://mde.maryland.gov/programs/air/
- **Expected Method**: Similar to Virginia (likely needs Playwright)
- **Priority**: HIGH (Frederick County data centers)

### Pennsylvania
- **Agency**: PA Department of Environmental Protection (DEP)
- **URL**: https://www.dep.pa.gov/
- **Expected Method**: May need Playwright
- **Priority**: HIGH (multiple facilities)

### New Jersey
- **Agency**: NJ Department of Environmental Protection
- **Focus**: GP-005 Generator General Permits
- **URL**: https://www.nj.gov/dep/aqpp/
- **Expected Method**: Possibly simpler (may work with requests)
- **Priority**: HIGH

### Ohio
- **Agency**: Ohio EPA
- **URL**: https://epa.ohio.gov/divisions-and-offices/air-pollution-control
- **Expected Method**: TBD
- **Priority**: MEDIUM

## Recommended Next Steps

### 1. Reconnaissance Phase
For each state, determine:
- [ ] Does the state have a dedicated data center permits page?
- [ ] What's the website structure? (table, list, search form?)
- [ ] Are PDFs directly accessible or embedded?
- [ ] Does simple `requests` work or need browser automation?
- [ ] How many permits are available?

### 2. Implementation Phases

#### Phase 1: Create Base Class
```python
class BasePermitScraper:
    """Base class for all state permit scrapers"""
    
    def __init__(self, state, output_dir, test_limit=None):
        self.state = state
        self.output_dir = Path(output_dir) / state
        self.test_limit = test_limit
        
    def fetch_permit_links(self, page):
        """Override in subclass"""
        raise NotImplementedError
        
    def download_permit(self, context, permit_info):
        """Common download logic - works for most states"""
        # Reuse Virginia's page.pdf() method
        pass
        
    def run(self, delay=1.5, headless=False):
        """Common execution flow"""
        # Reuse Virginia's structure
        pass
```

#### Phase 2: Implement State-Specific Scrapers
```python
class MarylandPermitScraper(BasePermitScraper):
    def __init__(self, output_dir="03_permit_documents/by_state/Documents", test_limit=None):
        super().__init__("Maryland", output_dir, test_limit)
        self.permits_url = "https://mde.maryland.gov/..."  # TBD
        
    def fetch_permit_links(self, page):
        # Maryland-specific link extraction
        pass
```

#### Phase 3: Create Unified Runner
```python
# multi_state_scraper.py
def main():
    states = {
        'virginia': VirginiaPermitScraper,
        'maryland': MarylandPermitScraper,
        'pennsylvania': PennsylvaniaPermitScraper,
        # ... etc
    }
    
    # Run scrapers for selected states
    for state_name, scraper_class in states.items():
        if state_name in args.states:
            scraper = scraper_class(test_limit=args.test_limit)
            scraper.run()
```

### 3. Testing Strategy
For each new state:
1. Test with `--test` flag (5 permits)
2. Verify PDF quality
3. Check success rate (target >90%)
4. Run full scrape if successful

## Code Reusability

### What Can Be Reused (90%)
- ✅ Browser setup and configuration
- ✅ PDF download/render logic
- ✅ File management and resume capability
- ✅ Error handling and retries
- ✅ Logging infrastructure
- ✅ Rate limiting
- ✅ Progress tracking

### What Needs Customization (10%)
- ❌ URL endpoints (state-specific)
- ❌ HTML selectors (different site structures)
- ❌ Pagination logic (if applicable)
- ❌ Authentication (if required)

## Expected Outcomes

### Total Permits Estimate
- Virginia: 188 (confirmed)
- Maryland: 50-100 (estimated)
- Pennsylvania: 100-150 (estimated)
- New Jersey: 30-50 (estimated)
- Others: 200-300 (estimated)
- **Total: 568-788 permits across all states**

### Development Time
- Base class: 2 hours
- Per state (simple): 1-2 hours
- Per state (complex): 3-4 hours
- **Total: 1-2 days for all 14 jurisdictions**

### Success Rate Target
- **Goal: >95% success rate per state**
- Virginia baseline: 100% ✓

## Tools Required
- `playwright` - Browser automation
- `beautifulsoup4` - HTML parsing (for simpler sites)
- `requests` - HTTP requests (for simpler sites)
- `pandas` - Data consolidation (optional)

## File Organization
```
05_scripts/data_collection/
├── base_permit_scraper.py          # Base class
├── virginia_deq_scraper.py         # ✓ DONE
├── maryland_mde_scraper.py         # TODO
├── pennsylvania_dep_scraper.py     # TODO
├── new_jersey_dep_scraper.py       # TODO
├── multi_state_runner.py           # Unified interface
└── README_MULTI_STATE.md           # This file
```

## Questions to Answer for Each State

1. **URL**: Where are the permits published?
2. **Format**: PDFs, HTML pages, or database?
3. **Access**: Public or authentication required?
4. **Structure**: Table, list, search interface?
5. **Volume**: How many permits?
6. **Challenges**: Bot protection? Captchas? Complex navigation?

## Next Action

**Start with Maryland** - likely similar structure to Virginia, high data center concentration.

1. Manually visit Maryland DEP air quality permits page
2. Identify URL pattern and HTML structure
3. Test if `requests` works or need Playwright
4. Create `maryland_mde_scraper.py` based on Virginia template
5. Test with 5 permits
6. Full run if successful

---

**Status**: Virginia complete, ready to expand to other states using proven Playwright approach.
