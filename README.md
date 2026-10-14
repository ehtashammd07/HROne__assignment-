# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking, manual regularization with audit logging, and analytical aggregations per business rules R1–R10.

## Requirements & Environment

- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0)

Connection details are read from environment variables:
- `MONGO_URI`: MongoDB connection string (defaults to `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (defaults to `attendance_db`)

All required indexes are created idempotently on application startup via the FastAPI lifespan hook.

## How to Run

1. Create and activate a virtual environment:
```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Start the server:
```bash
uvicorn app.main:app --port 8000
```

The service is available at `http://localhost:8000`. Interactive documentation is available at `http://localhost:8000/docs`.

## Project Structure

```
├── app/
│   ├── __init__.py
│   └── main.py          # Complete application code (FastAPI + PyMongo)
├── sample_data/         # Reference sample data shapes
├── requirements.txt     # Python dependencies
├── REVIEW.md            # Part A starter review and defect report
├── DECISIONS.md         # Part C architecture and database decisions
├── openapi.yaml         # OpenAPI 3.0 specification & contract
├── DATA_MODEL.md        # Stored data specifications
└── README.md            # Instructions and documentation
```

## Implementation Highlights

- **Part A (Defect Review):** Identified and resolved 21 defects in the starter code, including race conditions in punch-in and employee creation, off-by-one pagination, memory bloat in list queries, and improper rounding/overtime thresholds. Detailed findings are in `REVIEW.md`.
- **Part B (Business Rules & Analytics):**
  - Concurrency safety with atomic MongoDB updates and unique compound index constraints.
  - Full adherence to IST timezone conventions and overnight shift boundaries (R1).
  - High-performance aggregation pipelines for monthly summaries, cross-collection department summaries (including zero-log headcount), late-comers leaderboard with standard competition ranking (1, 2, 2, 4), and department daily trends with gap filling (`$densify`) and rolling 7-day averages (`$setWindowFields`).
- **Part C (Explain & Decisions):**
  - Query explainability endpoint at `/admin/explain/{endpoint}` running with `executionStats` verbosity.
  - Architectural rationale documented in `DECISIONS.md`.
