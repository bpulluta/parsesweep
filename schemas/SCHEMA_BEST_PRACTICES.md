# Schema Design Best Practices

## Overview
Your schema structure **directly determines** your spreadsheet output. Design schemas to match how you want to analyze the data.

## Golden Rule: Main Array = Spreadsheet Rows

**The main array in your schema becomes rows in the consolidated spreadsheet.**

```json
{
  "utility_info": { ... },           // ← Common context
  "rate_schedules": [                 // ← MAIN ARRAY = ROWS
    { "rate_name": "Schedule R", ... },  // ← Row 1
    { "rate_name": "Schedule C", ... }   // ← Row 2
  ]
}
```

**Result**: 2 rows (one per rate schedule)

## Nested Arrays: Expand vs. Summarize

The consolidator **automatically** decides based on size and consistency:

### ✅ Will Expand to Columns (Good for Analysis)
- **≤15 items** with **≤20 fields** each
- **Consistent structure** (same fields across all items)

```json
"charges": [
  {"type": "Customer", "rate": 7.1, "unit": "$"},
  {"type": "Energy", "rate": 0.1038, "unit": "$/kWh"}
]
```

**Result**: Separate columns for each charge:
- `Charges Customer Rate`, `Charges Customer Unit`
- `Charges Energy Rate`, `Charges Energy Unit`

### ✅ Will Summarize (Good for Context)
- **>15 items** OR **>20 fields**
- **Inconsistent structure**

```json
"permit_history": [
  {"date": "2023-01-15", "action": "Applied", ...},
  {"date": "2023-02-20", "action": "Approved", ...},
  ... 30 more items
]
```

**Result**: One column with summary:
`"Applied: 2023-01-15; Approved: 2023-02-20; ..."`

## Design Patterns

### Pattern 1: Entity-Based (Best for Most Use Cases)

✅ **Good**: One entity type per row
```json
{
  "utility_info": {...},
  "rate_schedules": [           // One row per schedule
    {
      "rate_name": "Residential",
      "charges": [...]           // Expands to columns
    }
  ]
}
```

❌ **Bad**: Multiple entity types mixed
```json
{
  "rates": [                     // Mix of different things
    {"type": "Customer charge", "rate": 7.1},
    {"type": "Energy charge", "rate": 0.1},
    {"type": "Demand charge", "rate": 5.0},
    ... 50 more charges
  ]
}
```
**Problem**: 50 rows of charges instead of organized schedules

### Pattern 2: Contextual Grouping

✅ **Include distinguishing info in nested objects**
```json
{
  "type": "Energy charge",
  "rate": 0.1038,
  "season": "Summer",           // ← Distinguisher
  "time_period": "On-peak"      // ← Distinguisher
}
```

**Result**: `Charges Energy Summer On-Peak Rate`

❌ **Lose distinguishing info**
```json
{
  "type": "Energy charge",
  "rate": 0.1038
  // Lost: which season? which period?
}
```

### Pattern 3: Consistent Field Structure

✅ **Same fields across items**
```json
"requirements": [
  {"category": "Setback", "value": 1320, "unit": "feet"},
  {"category": "Height", "value": 35, "unit": "feet"},
  {"category": "Noise", "value": 50, "unit": "dBA"}
]
```
**Result**: Clean expansion with consistent columns

❌ **Varying fields**
```json
"requirements": [
  {"category": "Setback", "distance": 1320, "from": "residence"},
  {"category": "Height", "max_height": 35},
  {"category": "Noise", "limit_day": 50, "limit_night": 45}
]
```
**Problem**: Inconsistent structure → summarized instead of expanded

## Optimization Tips

### For Data Analysis (Science/Modeling)
**Goal**: Structured columns with analyzable values

1. **Keep nested arrays small** (≤15 items)
2. **Use consistent field names** across all items
3. **Include distinguishing context** (season, tier, period)
4. **Separate values and units** into different fields

```json
"charges": [
  {
    "charge_type": "Energy",
    "rate": 0.1038,              // ← Numeric value
    "unit": "$/kWh",             // ← Separate unit
    "season": "Summer",          // ← Context
    "time_period": "On-peak"     // ← Context
  }
]
```

### For Documentation/Context
**Goal**: Readable summaries

1. **Use descriptive type/name fields**
2. **Keep value fields clear**
3. **Let large arrays summarize naturally**

```json
"comments": [
  {"author": "Inspector", "date": "2024-01-15", "text": "..."},
  ... many items ...
]
```

## Common Mistakes

### ❌ Mistake 1: Flat When Should Be Nested
```json
{
  "rate_name": "Schedule R",
  "customer_charge": 7.1,
  "energy_charge_summer": 0.1038,
  "energy_charge_winter": 0.0857
}
```
**Problem**: Hard to extend, loses structure

✅ **Better**: 
```json
{
  "rate_name": "Schedule R",
  "charges": [
    {"type": "Customer", "rate": 7.1},
    {"type": "Energy", "season": "Summer", "rate": 0.1038},
    {"type": "Energy", "season": "Winter", "rate": 0.0857}
  ]
}
```

### ❌ Mistake 2: Over-Nesting
```json
{
  "rate_schedules": [
    {
      "details": {
        "info": {
          "rate_name": "Schedule R"  // ← Too deep
        }
      }
    }
  ]
}
```
**Problem**: Creates messy column names

✅ **Better**: Keep 2 levels max
```json
{
  "rate_schedules": [
    {
      "rate_name": "Schedule R",
      "effective_date": "2024-01-01"
    }
  ]
}
```

### ❌ Mistake 3: Using Arrays for Single Values
```json
{
  "rate_name": ["Schedule R"]  // ← Array with one item
}
```
**Problem**: Unnecessary complexity

✅ **Better**: 
```json
{
  "rate_name": "Schedule R"
}
```

## Testing Your Schema

After creating a schema, test consolidation with sample data:

```bash
# Extract sample
pixi run streamline-extract extract sample.pdf --schema your_schema.json

# Check consolidation output  
pixi run streamline-extract consolidate extracted/

# Review the Excel/CSV:
# - Are there the right number of rows?
# - Are nested arrays expanded correctly?
# - Are column names clear?
```

## Summary

| Aspect | Guideline |
|--------|-----------|
| **Main array** | Represents the primary entity (one row per item) |
| **Nested arrays ≤15 items** | Will expand to columns (good for analysis) |
| **Nested arrays >15 items** | Will summarize (good for context) |
| **Field consistency** | Same fields across items → clean expansion |
| **Distinguishing context** | Include season/period/tier for clear column names |
| **Depth** | Keep 2 levels max for readable column names |

**Remember**: The consolidator is smart but not magic. Good schema design = good spreadsheet output.
