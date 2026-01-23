# StreamlineExtract API

**Production-ready REST API for universal document extraction.**

Extract structured data from any document type (PDFs, DOCX, TXT, XLSX, CSV) using AI-powered extraction with custom JSON schemas.

## 🚀 Quick Start

### 1. Start the API Server

```bash
./scripts/start_api.sh
```

Or manually:
```bash
pixi run uvicorn streamline_extract.api.main:app --reload
```

The API will be available at: **http://localhost:8000**

### 2. Access Interactive Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### 3. Make Your First Request

```bash
# Extract from a document
curl -X POST "http://localhost:8000/api/v1/extract" \
  -F "file=@your-document.pdf" \
  -F "schema_preset=geothermal_ordinance"
```

---

## 📚 API Endpoints

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information |
| `/health` | GET | Health check |
| `/api/v1/extract` | POST | Extract from single document |
| `/api/v1/extract/batch` | POST | Batch extraction (async) |
| `/api/v1/schemas` | GET | List available schemas |
| `/api/v1/schemas/{name}` | GET | Get specific schema |
| `/api/v1/metrics` | GET | Prometheus metrics |

---

## 🔧 Configuration

### Environment Variables

Create a `.env` file in the project root:

```bash
# Option 1: Use OpenAI
OPENAI_API_KEY=sk-your-key-here

# Option 2: Use Azure OpenAI (recommended for production)
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_MODEL=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2024-02-15-preview

# Optional: Enable debug mode
DEBUG=false
```

---

## 📖 Usage Examples

### Extract from Document

```python
import requests

# Upload a document for extraction
with open("document.pdf", "rb") as f:
    response = requests.post(
        "http://localhost:8000/api/v1/extract",
        files={"file": f},
        data={
            "schema_preset": "geothermal_ordinance",
            "enable_qa_qc": False,
            "model": "gpt-4o-mini"
        }
    )

result = response.json()
print(f"Extracted {len(result['data'])} items")
print(f"Cost: ${result['cost_usd']:.4f}")
```

### Batch Extraction

```python
files = [
    ("files", open("doc1.pdf", "rb")),
    ("files", open("doc2.pdf", "rb"))
]

response = requests.post(
    "http://localhost:8000/api/v1/extract/batch",
    files=files,
    data={"schema_preset": "electricity_tariff"}
)

batch = response.json()
print(f"Batch ID: {batch['batch_id']}")
print(f"Status: {batch['status']}")
```

### List Available Schemas

```python
response = requests.get("http://localhost:8000/api/v1/schemas")
schemas = response.json()["schemas"]

for schema in schemas:
    print(f"- {schema['name']}: {schema['description']}")
```

### Custom Schema

```python
custom_schema = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "number"}
                }
            }
        }
    }
}

with open("document.pdf", "rb") as f:
    response = requests.post(
        "http://localhost:8000/api/v1/extract",
        files={"file": f},
        json={"custom_schema": custom_schema}
    )
```

---

## 🏗️ Architecture

### Tech Stack

- **Framework**: FastAPI 0.109+
- **Server**: Uvicorn with hot reload
- **Validation**: Pydantic v2
- **Async Processing**: Celery + Redis (coming soon)
- **Monitoring**: Prometheus metrics (coming soon)

### Request Flow

```
Client → FastAPI → DocumentExtractor → OpenAI/Azure → Response
                ↓
         Background Tasks (optional)
                ↓
            Webhook Notification
```

---

## 🔐 Security

### API Key Authentication (Coming Soon)

```python
headers = {"Authorization": "Bearer your-api-key"}
response = requests.post(url, headers=headers, files=files)
```

### Rate Limiting (Coming Soon)

- Free tier: 100 requests/day
- Pro tier: 10,000 requests/day
- Enterprise: Unlimited

---

## 📊 Response Format

### Success Response

```json
{
  "job_id": "job_1234567890.123",
  "status": "completed",
  "data": {
    "requirements": [
      {
        "feature": "Setback distance",
        "value": "500",
        "unit": "feet"
      }
    ],
    "jurisdiction": {
      "state": "Colorado",
      "county": "Chaffee"
    }
  },
  "metadata": {
    "filename": "ordinance.pdf",
    "model": "gpt-4o-mini",
    "extraction_timestamp": "2026-01-22T10:30:00Z"
  },
  "completeness_score": 0.95,
  "cost_usd": 0.052,
  "processing_time": 25.3
}
```

### Error Response

```json
{
  "error": "Unsupported file format",
  "status_code": 400,
  "timestamp": "2026-01-22T10:30:00Z"
}
```

---

## 🧪 Testing

### Health Check

```bash
curl http://localhost:8000/health
```

### Test Extraction

```bash
curl -X POST "http://localhost:8000/api/v1/extract" \
  -F "file=@documents/geothermal_ordinances/test.pdf" \
  -F "schema_preset=geothermal_ordinance"
```

---

## 🚀 Deployment

### Docker (Coming Soon)

```bash
docker build -t streamline-extract-api .
docker run -p 8000:8000 --env-file .env streamline-extract-api
```

### Production Settings

```bash
# Use multiple workers for production
uvicorn streamline_extract.api.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --no-reload
```

---

## 📈 Monitoring

### Metrics Endpoint

```bash
curl http://localhost:8000/api/v1/metrics
```

### Prometheus Integration (Coming Soon)

```yaml
scrape_configs:
  - job_name: 'streamline-extract-api'
    static_configs:
      - targets: ['localhost:8000']
```

---

## 🔮 Roadmap

- [x] Basic REST API with FastAPI
- [x] Single document extraction
- [x] Schema management
- [x] Health checks
- [ ] API key authentication
- [ ] Rate limiting
- [ ] Async batch processing with Celery
- [ ] Webhook notifications
- [ ] Prometheus metrics
- [ ] Docker deployment
- [ ] Client SDKs (Python, JavaScript)
- [ ] OpenAPI client generation

---

## 📝 License

MIT License - See LICENSE file for details.

---

## 💬 Support

- **Documentation**: http://localhost:8000/docs
- **Issues**: GitHub Issues
- **Email**: support@streamlineextract.com (coming soon)

---

**Built with ❤️ using FastAPI and OpenAI**
