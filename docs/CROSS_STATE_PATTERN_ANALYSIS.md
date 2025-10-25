# Cross-State Permit Pattern Analysis

## Purpose
Analyze permit formats across Virginia and Illinois to determine if we need state-specific QA/QC logic or can build a unified, state-agnostic system.

## Analysis Date
October 24, 2025

---

## Permits Analyzed

### Virginia
1. **11790** - DP Facilities Inc. South, LLC (6+1 generators)
2. **11541** - Southwest Enterprise Solutions Center (3 generators)  
3. **51232** - Quality Investment Properties Richmond, LLC (multiple generator sets)

### Illinois
1. **031449AEJ** - Compass Datacenters (200 + 5 generators)
2. **031600GNA** - Equinix LLC (6 + 4 generators)

---

## Key Findings

### 1. Generator Identification Patterns

#### Virginia Style
```
Format: Alphanumeric with ranges or individual units
Examples:
- "EG01-EG06" (range notation for 6 generators)
- "EG07" (single generator)
- "3", "2", "1" (numeric only, older permits)
- "EG77-EG79" (Three generators)
- "EG04-EG05" (Two generators)

Pattern: Mix of alphanumeric prefixes (EG, Gen) + numbers, with ranges using "-"
```

#### Illinois Style
```
Format: Alphanumeric with ranges
Examples:
- "G-1 thru G-6" (range notation)
- "G-7 thru G-10" (range notation)
- Description-based: "Two hundred (200) 2,000 kWe generators"
- Description-based: "Five (5) 1,250 kWe generators"

Pattern: "G-#" notation or quantity-based descriptions
         Uses "thru" instead of "-" for ranges
```

**Commonality**: ✅ Both use reference numbers (alphanumeric or numeric)
**Difference**: ⚠️ Range notation differs ("EG01-EG06" vs "G-1 thru G-6")

---

### 2. Capacity Specifications

#### Virginia Style
```
Format: Value + unit in running text or table
Examples:
- "2500 kW (4060 hp)"
- "1500 KW diesel-powered ... 2500 brake horsepower"
- "2,500 kW, each" and "3,674 BHP, each"

Pattern: Inline or table format with both kW and HP/BHP commonly listed
```

#### Illinois Style  
```
Format: Value + unit in permit description
Examples:
- "2,000 kWe (2,913 engine hp)"
- "1,250 kWe (1,877 engine hp)"
- "1,500 kW (2,153 HP)"
- "2,000 kW (2,897 HP)"

Pattern: Inline format, uses "kWe" (electric) notation, "engine hp"
```

**Commonality**: ✅ Both specify kW and HP, values extractable with same regex
**Difference**: ⚠️ Illinois uses "kWe" and "engine hp", Virginia uses "kW" and "bhp/hp"

---

### 3. Emission Limits Format

#### Virginia Style (11541 Example)
```
Format: Stacked table or list format
Example:
  Sulfur Dioxide           4.20 lbs/hr    1.05 tons/yr
  Nitrogen Oxides (as NO2) 63.90 lbs/hr   15.96 tons/yr
  Carbon Monoxide          13.77 lbs/hr   3.44 tons/yr
  Particulate Matter(PM-10) 4.49 lbs/hr   1.12 tons/yr
  Volatile Organic Compounds 5.07 lbs/hr  1.27 tons/yr

Pattern: Pollutant name, lbs/hr, tons/yr in aligned columns
```

#### Illinois Style (031449 Example)
```
Format: Table with load-based emissions
Example:
  Engine Load    100%   75%    50%    25%    0%    TPY
  NOx (lb/hr)    14.80  13.04  11.06  9.87   4.37  17.16
  CO (lb/hr)     4.14   4.42   3.05   3.11   8.70  0.45
  PM (lb/hr)     0.57   0.59   0.61   0.70   1.37  0.08
  VOM (lb/hr)    0.54   0.55   0.53   0.58   1.27  0.07
  SO2 (lb/hr)    0.001  0.001  0.001  0.001  0.001 0.001

Pattern: Complex table with multiple load scenarios
```

**Commonality**: ✅ Both specify pollutant limits in lbs/hr and tons/yr
**Difference**: ⚠️⚠️ **MAJOR** - Illinois uses load-based tables, Virginia uses simple limits

---

### 4. Operating Hours Limits

#### Virginia Style
```
Format: Plain language in conditions
Examples:
- "shall not operate more than 500 hours per year"
- "not operate more than 500 hours per year each"
- "≤218 hrs/yr"

Pattern: Straightforward annual hour limit
```

#### Illinois Style
```
Format: Often broken down by purpose
Examples:
- "100 hours per calendar year for maintenance and testing"
- "50 hours per calendar year in non-emergency situations"
- "177.5 hours per year" (combined for group)

Pattern: May specify hours by operational category
```

**Commonality**: ✅ Both specify annual hour limits
**Difference**: ⚠️ Illinois may split hours by purpose (testing vs non-emergency)

---

### 5. Fuel Specifications

#### Virginia Style
```
Format: Conditions with explicit sulfur content
Examples:
- "distillate oil ... maximum sulfur content of 0.5 weight percent"
- "No. 2 distillate oil with sulfur content not exceeding 0.0015% by weight"
- "sulfur content ≤ 0.5%"

Pattern: Percentage or decimal, explicit fuel grade
```

#### Illinois Style
```
Format: References to federal standards
Examples:
- "diesel fuel that meets the requirements of 40 CFR 1090.305"
- "maximum sulfur content of 15 ppm"
- "sulfur content of 0.0015%"

Pattern: Often cites CFR regulations, uses ppm
```

**Commonality**: ✅ Both specify sulfur limits
**Difference**: ⚠️ Illinois more likely to cite federal regs, uses ppm vs percent

---

### 6. Pollutant Nomenclature

#### Virginia
```
- "Nitrogen Oxides (as NO2)" or "NOx"
- "Particulate Matter (PM-10)" or "PM-10"
- "Volatile Organic Compounds" or "VOC"
- "Carbon Monoxide" or "CO"
- "Sulfur Dioxide" or "SO2"
```

#### Illinois
```
- "NOx" or "Nitrogen Oxides"
- "PM" or "PM-10" or "Particulate Matter"
- "VOM" (Volatile Organic Material) instead of "VOC"
- "CO" or "Carbon Monoxide"
- "SO2" or "Sulfur Dioxide"
```

**Commonality**: ✅ Most pollutants use standard abbreviations
**Difference**: ⚠️⚠️ **CRITICAL** - Illinois uses "VOM" not "VOC"!

---

### 7. Control Technology

#### Virginia Style
```
Format: Descriptive conditions
Examples:
- "controlled by turbocharged engines, charge air coolers, and the use of good operating practices"
- "turbocharged engine and aftercooler"

Pattern: Narrative description in permit conditions
```

#### Illinois Style
```
Format: References to federal standards (NSPS, NESHAP)
Examples:
- "subject to 40 CFR 60 Subpart IIII"
- "subject to 40 CFR 63 Subpart ZZZZ"
- "Tier 2 emission standards"

Pattern: Federal regulation citations more common than descriptions
```

**Commonality**: ✅ Both may mention control technology
**Difference**: ⚠️ Illinois focuses on regulatory compliance, Virginia on equipment

---

## Critical Differences Summary

### 🚨 HIGH IMPACT (Requires State-Aware Logic)

1. **VOM vs VOC**: Illinois calls it "VOM" (Volatile Organic Material), Virginia uses "VOC" (Volatile Organic Compounds)
   - **Impact**: Field mapping must handle this alias
   - **Solution**: Map VOM → VOC in extraction

2. **Emission Table Format**: Illinois uses complex load-based tables, Virginia uses simple limit lists
   - **Impact**: Illinois requires parsing multi-column tables with load percentages
   - **Solution**: LangExtract examples must cover both formats

3. **Range Notation**: "G-1 thru G-6" (IL) vs "EG01-EG06" (VA)
   - **Impact**: Different parsing logic for range expansion
   - **Solution**: Regex must handle both "thru" and "-"

### ⚠️ MEDIUM IMPACT (Flexible Extraction Can Handle)

4. **Sulfur Content Units**: ppm (IL) vs percent (VA)
   - **Impact**: Need unit conversion
   - **Solution**: LangExtract can extract with units, converter handles ppm/percent

5. **Hours by Purpose**: IL splits maintenance vs non-emergency, VA gives total
   - **Impact**: May need to sum multiple hour limits in Illinois
   - **Solution**: Extract all hour mentions, take maximum or sum

6. **Capacity Notation**: "kWe" (IL) vs "kW" (VA), "engine hp" vs "bhp"
   - **Impact**: Slight variation in unit labels
   - **Solution**: Regex handles both, normalize in post-processing

### ✅ LOW IMPACT (Already Handled)

7. **Reference Number Patterns**: Both use alphanumeric, just different prefixes
8. **Pollutant Names**: Standard abbreviations work across states
9. **Fuel Types**: Both use "distillate", "diesel", "No. 2 fuel oil"

---

## Recommended Approach

### Option 1: State-Specific QA/QC (NOT RECOMMENDED)
```python
if state == "Virginia":
    examples = virginia_examples
    voc_field = "vocEmissionLimit"
elif state == "Illinois":
    examples = illinois_examples
    voc_field = "vocEmissionLimit"  # Map VOM to VOC
```

**Pros**: Maximum precision per state  
**Cons**: 
- Maintenance nightmare (50 states!)
- Requires state detection
- Code duplication
- Doesn't handle multi-state facilities

### Option 2: Unified Multi-Pattern System (RECOMMENDED ✅)
```python
# Single system with comprehensive pattern coverage
examples = [
    # Virginia-style range notation
    "EG01-EG06 (6) generators",
    # Illinois-style range notation  
    "G-1 thru G-6 generators",
    # Simple emission limits (VA style)
    "NOx: 53.7 lbs/hr, 83.75 tons/yr",
    # Table-based limits (IL style)
    "NOx at 100% load: 14.80 lb/hr, Annual: 17.16 TPY",
    # VOC/VOM handling
    "VOC: 1.29 lbs/hr" / "VOM: 0.54 lb/hr",
]

# Pollutant aliasing
POLLUTANT_ALIASES = {
    'voc': ['voc', 'vocs', 'vом', 'volatile organic'],
    'pm': ['pm', 'particulate', 'particulate matter'],
    'nox': ['nox', 'no_x', 'nitrogen oxides'],
}
```

**Pros**:
- Single codebase for all states
- Automatically handles new states
- Examples cover all pattern variations
- Robust to permit format changes

**Cons**:
- Slightly more complex examples
- May need more examples total

---

## Implementation Strategy

### Phase 1: Enhance Current System (Unified Approach)

1. **Expand LangExtract Examples** (30 min)
   - Add Illinois-style table format examples
   - Add "G-# thru G-#" range notation examples
   - Add VOM/VOC variations

2. **Add Pollutant Aliasing** (15 min)
   ```python
   POLLUTANT_ALIASES = {
       'voc': ['voc', 'vом', 'volatile organic compound', 'volatile organic material'],
       'pm': ['pm', 'particulate matter', 'particulate'],
       # ... etc
   }
   ```

3. **Enhance Range Notation Parser** (20 min)
   ```python
   def parse_range(ref_num):
       # Handle "EG01-EG06"
       if '-' in ref_num and 'thru' not in ref_num.lower():
           return parse_dash_range(ref_num)
       # Handle "G-1 thru G-6"  
       elif 'thru' in ref_num.lower():
           return parse_thru_range(ref_num)
       else:
           return [ref_num]
   ```

4. **Unit Normalization** (15 min)
   ```python
   def normalize_sulfur(value, unit):
       if unit == 'ppm':
           return value / 10000  # ppm to decimal
       elif unit == '%':
           return value / 100     # percent to decimal
       return value
   ```

### Phase 2: Validation Across States (1 hour)

1. Test on Virginia permits (11790, 11541, 51232)
2. Test on Illinois permits (031449, 031600)
3. Compare accuracy across states
4. Tune examples based on results

### Phase 3: Documentation (30 min)

1. Document pattern variations in README
2. Update examples with state annotations
3. Create state-specific gotchas guide

---

## Decision Matrix

| Criterion | State-Specific | Unified Multi-Pattern | Winner |
|-----------|----------------|----------------------|---------|
| **Maintainability** | ❌ 50 codebases | ✅ 1 codebase | Unified |
| **Scalability** | ❌ Linear growth | ✅ Constant | Unified |
| **Accuracy** | ⚠️ High per state | ⚠️ Good overall | Tie |
| **Development Time** | ❌ High | ✅ Moderate | Unified |
| **Testing Burden** | ❌ 50x tests | ✅ Comprehensive | Unified |
| **Code Complexity** | ⚠️ Duplicated | ✅ Centralized | Unified |

**Recommendation**: **Unified Multi-Pattern System** wins 5-0-2

---

## Risk Assessment

### Unified Approach Risks

1. **Risk**: Examples become too generic, lose precision
   - **Mitigation**: Use specific, real-world examples from each state
   - **Severity**: Low

2. **Risk**: Pattern conflicts between states
   - **Mitigation**: Priority ordering in regex, most specific first
   - **Severity**: Low

3. **Risk**: State-specific edge cases missed
   - **Mitigation**: Iterative improvement, add examples as found
   - **Severity**: Medium

4. **Risk**: Performance degradation with many examples
   - **Mitigation**: LangExtract handles 10-20 examples efficiently
   - **Severity**: Low

### State-Specific Approach Risks

1. **Risk**: Maintenance nightmare with 50 states
   - **Severity**: **CRITICAL** ⚠️⚠️⚠️

2. **Risk**: Code drift between states
   - **Severity**: High

3. **Risk**: Difficult to add new states
   - **Severity**: High

---

## Conclusion

**BUILD A UNIFIED, STATE-AGNOSTIC SYSTEM** with comprehensive pattern coverage.

### Why This Works

1. **Core data is consistent**: All permits have generators with capacities, emissions, and operating limits
2. **Format variations are finite**: Tables vs lists, range notations, unit variations
3. **Aliasing handles naming**: VOM→VOC, PM-10→PM10, etc.
4. **LangExtract is flexible**: Can learn multiple patterns from examples
5. **Scalability is paramount**: 50 states + territories + evolving formats

### Critical Implementation Points

1. ✅ **Add VOM/VOC aliasing** - Most critical for Illinois
2. ✅ **Support both range formats** - "EG01-EG06" and "G-1 thru G-6"
3. ✅ **Handle table-based emissions** - Illinois load-based format
4. ✅ **Unit normalization** - ppm vs percent, kWe vs kW
5. ✅ **Flexible regex** - Match variations without over-fitting

### Success Criteria

- ✅ 80%+ field coverage across BOTH states
- ✅ Same codebase handles VA and IL permits
- ✅ Easy to add new state examples without code changes
- ✅ Performance remains <$0.01/permit
- ✅ Accuracy remains >85% across all states

---

## Next Steps (DO NOT IMPLEMENT YET)

**HOLD** until user approves this analysis:

1. Review this analysis with stakeholder
2. Confirm unified approach is correct
3. Then implement Phase 1 enhancements
4. Test across both states
5. Iterate based on results

**Question for User**: Does this analysis make sense? Should we proceed with the unified multi-pattern approach?
