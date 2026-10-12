# Employee Attendance & Analytics API

HROne Engineering Assignment — FastAPI + MongoDB

## Quick Start

```bash
# 1. Create venv and install deps
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure MongoDB
cp .env.example .env
# Edit .env with your MONGO_URI and MONGO_DB

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Run server
uvicorn app.main:app --port 8000
```

Server starts on `http://localhost:8000`. Health check: `GET /health` returns 200 when MongoDB is reachable.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

Real environment variables take priority over `.env`.

## API Endpoints

### System
- `GET /health` — 200 if MongoDB reachable, else 503

### Employees
- `POST /employees` — Create employee (201, 409 duplicate, 422 validation)
- `GET /employees` — List with `department` filter, pagination (`page`, `page_size` max 100), sorted by `emp_code`

### Attendance
- `POST /attendance/punch-in` — Record punch-in (race-safe via unique index)
- `POST /attendance/punch-out` — Record punch-out, computes work_hours/overtime/half_day
- `GET /attendance` — List with filters: `emp_code`, `date_from`, `date_to`, `status`; sorted date desc, emp_code asc; paginated
- `PATCH /attendance/{emp_code}/{date}` — Regularize: change status/punch times, recomputes derived fields, appends history entry

### Analytics (all backed by MongoDB aggregation pipelines)
- `GET /analytics/employees/{emp_code}/monthly?month=YYYY-MM` — Employee monthly summary
- `GET /analytics/departments/summary?month=YYYY-MM&department=` — Department summary (headcount includes zero-log employees per R9)
- `GET /analytics/leaderboard/late?month=YYYY-MM&limit=10&department=` — Late leaderboard with standard competition ranking (1,2,2,4)
- `GET /analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD` — Daily trend with gap-filling ($densify) and 7-day moving average

### Admin
- `GET /admin/explain/{endpoint}` — MongoDB explain with executionStats for the endpoint's main pipeline. Accepts same query params as the endpoint. Verifies IXSCAN, no COLLSCAN.

## Business Rules (R1–R10)
- R1: IST timezone, attendance date from punch_in (overnight shifts handled)
- R2: Late >10 min after shift_start, floor minutes from shift_start
- R3: Overtime >=30 min after shift_end, floor minutes
- R4: Work hours = (punch_out - punch_in)/3600, half-up 2 decimals
- R5: Half day if rounded work_hours < 4.50
- R6: PRESENT/WFH/ON_DUTY = present
- R7: Working days Mon-Fri from joined_on
- R8: Half-up rounding (2 decimals, 4 for rates)
- R9: Headcount = employees with joined_on <= period end
- R10: Page starts at 1, page_size max 100

## Implementation Notes
- All code in `app/main.py` (single file per requirements)
- Indexes created at startup (idempotent)
- Epoch milliseconds ↔ UTC BSON datetime conversion at API boundary
- Race safety: unique index on (emp_code, date) for punch-in; optimistic locking via history length for regularize
- Half-up rounding implemented via `math.floor(x*100 + 0.5)/100`
- Overnight shifts (shift_end <= shift_start) handled per R1
- $densify with `bounds` used for gap-filling in department_trend
- $rank used for standard competition ranking in leaderboard

## Known Limitations
- Regularize endpoint: concurrent PRESENT-with-times updates may hit optimistic lock retry (returns 409)
- Synchronous PyMongo; for 100x scale would migrate to Motor/async