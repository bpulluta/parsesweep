# StreamlineExtract - Refactoring & API Development Plan

## Executive Summary

Transform StreamlineExtract from a dual-purpose tool (geothermal ordinances + tariffs) into a **truly universal document extraction system** with a clean API for programmatic access.

### Current State Analysis

**What Works Well:**
- ✅ Core extraction engine (OpenAI Structured + LangExtract QA/QC)
- ✅ Universal consolidation logic
- ✅ Auto-detection of schema types
- ✅ Multi-format support (PDF, DOCX, TXT, XLSX, CSV)
- ✅ Clean CLI interface

**What Needs Cleaning:**
- ❌ Domain-specific code references (permits, gensets, generators) scattered throughout
- ❌ `scrapers/` directory (permit-specific, not part of core product)
- ❌ Text optimizer has permit-specific logic
- ❌ Deduplicator has permit-number extraction logic
- ❌ Validation utils use permit terminology
- ❌ Some docstrings mention specific domains instead of being generic

---

## Phase 1: Code Cleanup & Universalization

### 1.1 Text Optimizer Refactoring
**File:** `src/streamline_extract/extraction/text_optimizer.py`

**Current Issues:**
- Class named `PermitTextOptimizer` (domain-specific)
- Comments reference "generators", "permit details"
- Hardcoded patterns for "permit number", "generator specs"

**Refactoring Strategy:**
- Rename: `PermitTextOptimizer` → `DocumentTextOptimizer`
- Make all patterns configurable via schema hints
- Replace hardcoded domain keywords with generic "high_priority_terms" from schema
- Keep section detection logic but make it universal

**Benefits:**
- Works for any document type
- Schema can specify important keywords to preserve
- More efficient text optimization across domains

---

### 1.2 Deduplicator Refactoring
**File:** `src/streamline_extract/extraction/deduplicator.py`

**Current Issues:**
- Docstring says "duplicate permits"
- `_extract_permit_number()` is permit-specific
- Variable names use "permit" terminology

**Refactoring Strategy:**
- Rename variables: `permit_number` → `document_id`
- Generalize `_extract_permit_number()` to `_extract_document_id()` with configurable regex patterns
- Accept ID patterns from schema or config
- Make it work for any document with unique IDs (tariff IDs, ordinance numbers, etc.)

**Benefits:**
- Works for any document type with unique identifiers
- Configurable ID extraction patterns
- Maintains duplicate detection across all domains

---

### 1.3 Validation Utils Refactoring
**File:** `src/streamline_extract/extraction/validation_utils.py`

**Current Issues:**
- Functions use `permit_number` parameter names
- References to "generatorSets" specific field
- HTML reports say "Permit" in titles

**Refactoring Strategy:**
- Replace `permit_number` with `entity_id` or `document_id`
- Make validation generic - detect main array fields dynamically
- Update HTML templates to use generic terminology
- Keep the validation logic intact (it's already good)

**Benefits:**
- Validation works for any extracted data structure
- Reports are domain-agnostic
- Same QA/QC quality across all document types

---

### 1.4 Document Extractor Polish
**File:** `src/streamline_extract/extraction/document_extractor.py`

**Current Issues:**
- Docstring mentions "permits, ordinances, regulations" (listing specific types)
- Some comments reference "generator_id", "generator specs"
- Otherwise already quite generic!

**Refactoring Strategy:**
- Update docstrings to say "any structured document"
- Replace specific field names in comments with generic examples
- Ensure all ID field detection is fully dynamic (already mostly is)

**Benefits:**
- Clear messaging that this works universally
- No mental overhead from domain-specific examples

---

### 1.5 QA/QC Module Polish
**File:** `src/streamline_extract/extraction/qa_qc.py`

**Current Issues:**
- Comments mention "generatorSets, requirements, rates"
- Some field names hardcoded like `permitNumber`, `facilityName`

**Refactoring Strategy:**
- Make field detection fully dynamic (already mostly is)
- Remove hardcoded field names - detect from schema
- Update comments to be domain-agnostic

---

### 1.6 Scrapers Directory Decision
**Directory:** `src/streamline_extract/scrapers/`

**Analysis:**
- Contains permit-specific scrapers (Virginia, Uptime Institute)
- Not part of core document extraction product
- Adds confusion about product scope

**Decision Options:**

**Option A: Move to Archive** ✅ RECOMMENDED
```bash
mkdir -p archive/scrapers
mv src/streamline_extract/scrapers archive/
```
- Clean separation of concerns
- Scrapers available if needed later
- Core product is pure extraction

**Option B: Separate Package**
- Move to optional `streamline-extract-scrapers` package
- Keep codebase focused on extraction only

**Option C: Keep but Mark as Deprecated**
- Add DEPRECATED notice
- Don't actively maintain
- Remove in v2.0

**Recommendation:** Option A - Archive it. Scrapers are not core to the universal extraction vision.

---

## Phase 2: API Architecture Design

### 2.1 API Design Philosophy

**Principles:**
1. **Simple First:** Easy to use for common cases
2. **Powerful When Needed:** Advanced options for complex scenarios
3. **Stateless:** No server-side session management
4. **Scalable:** Can handle concurrent requests
5. **Observable:** Built-in logging, monitoring hooks
6. **Type-Safe:** Clear schemas for all inputs/outputs

---

### 2.2 API Technology Stack

**Framework:** FastAPI
- Modern, fast (high performance)
- Automatic OpenAPI docs
- Built-in validation with Pydantic
- Async support for scalability
- Perfect for LLM-based services

**Additional Libraries:**
- `pydantic` - Request/response validation
- `celery` + `redis` - Async job processing for long documents
- `boto3` - S3 integration for document storage (optional)
- `prometheus-client` - Metrics export

---

### 2.3 API Endpoints Design

#### **Basic Extraction API**

```python
POST /api/v1/extract
```

**Request:**
```json
{
  "document": {
    "content": "base64_encoded_document",  // or
    "url": "https://example.com/doc.pdf",  // or
    "s3_uri": "s3://bucket/key"
  },
  "schema": {
    "type": "json_schema",  // Inline JSON schema
    "preset": "geothermal_ordinance",  // or use preset
    "auto_detect": true  // or auto-detect from content
  },
  "options": {
    "enable_qa_qc": true,
    "max_context_chars": 400000,
    "model": "gpt-4o-mini"
  }
}
```

**Response:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",  // or "processing", "failed"
  "result": {
    "data": { /* extracted JSON */ },
    "completeness_score": 0.95,
    "processing_time": 12.5,
    "cost": 0.0034,
    "validation_report": { /* QA/QC details */ }
  },
  "metadata": {
    "document_type": "pdf",
    "pages": 45,
    "schema_detected": "geothermal_ordinance"
  }
}
```

---

#### **Batch Extraction API**

```python
POST /api/v1/extract/batch
```

**Request:**
```json
{
  "documents": [
    { "url": "https://example.com/doc1.pdf" },
    { "url": "https://example.com/doc2.pdf" }
  ],
  "schema": { "preset": "tariff" },
  "callback_url": "https://yourapp.com/webhook"  // Optional webhook
}
```

**Response:**
```json
{
  "batch_id": "batch_123",
  "status": "processing",
  "total_documents": 2,
  "completed": 0,
  "estimated_completion": "2026-01-22T15:30:00Z"
}
```

---

#### **Consolidation API**

```python
POST /api/v1/consolidate
```

**Request:**
```json
{
  "extractions": [
    { "data": { /* extraction 1 */ } },
    { "data": { /* extraction 2 */ } }
  ],
  "output_format": "excel",  // or "csv", "json"
  "deduplication": true
}
```

**Response:**
```json
{
  "consolidated_data": { /* flattened data */ },
  "download_url": "https://api.streamline.io/downloads/consolidated_123.xlsx",
  "stats": {
    "total_rows": 156,
    "duplicates_removed": 12,
    "schemas_merged": 1
  }
}
```

---

#### **Schema Endpoints**

```python
GET /api/v1/schemas
GET /api/v1/schemas/{preset_name}
POST /api/v1/schemas/validate
```

List available schema presets, get specific schema, validate custom schema.

---

#### **Health & Monitoring**

```python
GET /api/v1/health
GET /api/v1/metrics
```

Service health check and Prometheus metrics endpoint.

---

### 2.4 API Security

**Authentication:**
- API key authentication (simple, effective)
- JWT tokens for user-based access
- Rate limiting per API key

**Authorization:**
- Role-based access control (RBAC)
- Quota management (requests per month, cost limits)

**Data Security:**
- TLS/HTTPS only
- Document encryption at rest (if using S3)
- Automatic document deletion after processing
- No logging of sensitive document content

---

### 2.5 Async Processing Architecture

**For Long Documents:**

```
Client Request → API → Job Queue (Redis/Celery) → Worker Pool → Result Store → Callback/Polling
```

**Benefits:**
- No request timeouts for long documents
- Horizontal scalability (add more workers)
- Progress tracking
- Failed job retry logic

**Webhook Support:**
- Client provides callback URL
- API POSTs results when complete
- Retry logic for failed webhooks

---

## Phase 3: Implementation Roadmap

### Sprint 1: Code Cleanup (Week 1)
- [ ] Refactor text_optimizer.py
- [ ] Refactor deduplicator.py  
- [ ] Refactor validation_utils.py
- [ ] Polish document_extractor.py and qa_qc.py
- [ ] Archive scrapers directory
- [ ] Update all docstrings to be domain-agnostic
- [ ] Run full test suite and fix any breaks

### Sprint 2: API Foundation (Week 2)
- [ ] Set up FastAPI project structure
- [ ] Implement basic `/extract` endpoint
- [ ] Add schema management endpoints
- [ ] Create Pydantic models for all requests/responses
- [ ] Add API key authentication
- [ ] Write API tests

### Sprint 3: Advanced Features (Week 3)
- [ ] Implement batch processing with Celery
- [ ] Add webhook support
- [ ] Create consolidation endpoint
- [ ] Add rate limiting and quotas
- [ ] Implement health checks and monitoring
- [ ] Set up Prometheus metrics

### Sprint 4: Production Polish (Week 4)
- [ ] Add comprehensive error handling
- [ ] Create API documentation (OpenAPI/Swagger)
- [ ] Write client libraries (Python SDK)
- [ ] Performance optimization
- [ ] Load testing
- [ ] Deploy to production

---

## Phase 4: Project Structure (After Refactoring)

```
StreamlineExtract/
├── src/streamline_extract/
│   ├── api/                    # NEW: FastAPI application
│   │   ├── __init__.py
│   │   ├── main.py            # FastAPI app
│   │   ├── routes/
│   │   │   ├── extract.py
│   │   │   ├── consolidate.py
│   │   │   ├── schemas.py
│   │   │   └── health.py
│   │   ├── models/            # Pydantic models
│   │   │   ├── requests.py
│   │   │   └── responses.py
│   │   ├── auth/              # Authentication
│   │   ├── workers/           # Celery workers
│   │   └── config.py
│   │
│   ├── extraction/            # CLEANED: No domain-specific code
│   │   ├── document_extractor.py  # ✅ Generic
│   │   ├── document_optimizer.py  # RENAMED from text_optimizer
│   │   ├── document_deduplicator.py  # RENAMED
│   │   ├── validation_engine.py   # RENAMED from validation_utils
│   │   └── qa_qc.py              # ✅ Already generic
│   │
│   ├── consolidation/         # Already clean!
│   ├── cli/                   # Existing CLI (keep as is)
│   └── utils/
│
├── schemas/                   # Schema library
├── tests/
│   ├── test_extraction/
│   ├── test_consolidation/
│   └── test_api/             # NEW: API tests
│
├── archive/                   # OLD CODE
│   └── scrapers/             # Moved here
│
└── docs/
    ├── api/                   # NEW: API documentation
    │   ├── quickstart.md
    │   ├── authentication.md
    │   ├── endpoints.md
    │   └── examples.md
    └── development/
```

---

## Success Metrics

### Code Quality
- ✅ Zero domain-specific terminology in core extraction code
- ✅ 90%+ test coverage maintained
- ✅ All CI/CD checks passing
- ✅ Clean linting (no warnings)

### API Quality  
- ✅ < 100ms response time for health checks
- ✅ < 30s for typical document extraction
- ✅ 99.9% uptime SLA
- ✅ Comprehensive OpenAPI documentation
- ✅ Client libraries for Python, JavaScript

### User Experience
- ✅ One-line API call for basic extraction
- ✅ Clear error messages with actionable suggestions
- ✅ Progress tracking for long documents
- ✅ Predictable pricing (cost per document)

---

## Next Steps

1. **Review & Approve** this plan
2. **Start Sprint 1** - Code cleanup
3. **Set up API development environment**
4. **Define API pricing model** (if commercializing)
5. **Create API beta program** for early users

---

## Questions to Consider

1. **Deployment Strategy:** 
   - Self-hosted? Cloud (AWS/GCP/Azure)? Managed service?
   
2. **Pricing Model:**
   - Pay-per-document? Subscription? Free tier?
   
3. **Target Users:**
   - Developers (API)? Non-technical users (UI)? Both?
   
4. **Document Storage:**
   - Process and discard? Store for reprocessing? User choice?

5. **Multi-tenancy:**
   - Single-tenant? Multi-tenant with data isolation?

---

**This refactoring positions StreamlineExtract as a best-in-class universal document extraction system with a clean, modern API ready for scale.**
