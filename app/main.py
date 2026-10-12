"""
Employee Attendance & Analytics API - STARTER (FIXED)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, field_validator, EmailStr, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Datetime conversion helpers (API boundary: epoch ms int <-> MongoDB BSON datetime UTC)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert timezone-aware datetime to epoch milliseconds (truncated to seconds per R1)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC).replace(microsecond=0)
    return int(dt.timestamp() * 1000)


def ist_date_from_dt(dt: datetime) -> str:
    """Get IST calendar date (YYYY-MM-DD) from a UTC datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ist_dt = dt.astimezone(IST)
    return ist_dt.date().isoformat()


def shift_start_dt_on_date(shift_start: str, date_str: str) -> datetime:
    """Get shift start as UTC datetime for a given attendance date (IST)."""
    h, m = map(int, shift_start.split(":"))
    ist_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    return ist_dt.astimezone(UTC)


def shift_end_dt_on_date(shift_end: str, date_str: str, is_overnight: bool) -> datetime:
    """Get shift end as UTC datetime for a given attendance date (IST).
    For overnight shifts, shift ends on the NEXT calendar day."""
    h, m = map(int, shift_end.split(":"))
    if is_overnight:
        base_date = datetime.fromisoformat(date_str) + timedelta(days=1)
    else:
        base_date = datetime.fromisoformat(date_str)
    ist_dt = base_date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    return ist_dt.astimezone(UTC)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift is overnight (shift_end <= shift_start)."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh * 60 + em) <= (sh * 60 + sm)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Compute attendance date per R1: IST date of punch_in, except overnight shifts
    where punch_in earlier than shift_end belongs to previous day's shift."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    punch_in_time = punch_in_ist.time()
    overnight = is_overnight_shift(shift_start, shift_end)
    
    if overnight:
        eh, em = map(int, shift_end.split(":"))
        shift_end_time = punch_in_ist.replace(hour=eh, minute=em, second=0, microsecond=0).time()
        if punch_in_time < shift_end_time:
            # Belongs to previous day's shift
            return (punch_in_ist - timedelta(days=1)).date().isoformat()
    
    return punch_in_ist.date().isoformat()


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (R1)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Helpers (compute derived fields per R2-R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    """R2: Late if strictly more than 10 min after shift_start.
    late_minutes = floor minutes since shift_start (not since grace period end).
    Handles overnight shifts by using the attendance date, not the punch-in date."""
    punch_in = truncate_to_seconds(punch_in)
    attendance_date = compute_attendance_date(punch_in, shift_start, shift_end)
    shift_start_dt = shift_start_dt_on_date(shift_start, attendance_date)
    diff_seconds = (punch_in - shift_start_dt).total_seconds()
    if diff_seconds <= 600:  # 10 minutes = 600 seconds, strictly more than 10 min
        return 0
    minutes = math.floor(diff_seconds / 60)
    return minutes


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    punch_in = truncate_to_seconds(punch_in)
    punch_out = truncate_to_seconds(punch_out)
    hours = (punch_out - punch_in).total_seconds() / 3600
    return math.floor(hours * 100 + 0.5) / 100


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """R3: Overtime = floor minutes from shift_end to punch_out, only if >= 30 else 0.
    Handles overnight shifts (shift ends next calendar day)."""
    punch_out = truncate_to_seconds(punch_out)
    overnight = is_overnight_shift(shift_start, shift_end)
    shift_end_dt = shift_end_dt_on_date(shift_end, date_str, overnight)
    diff_seconds = (punch_out - shift_end_dt).total_seconds()
    minutes = math.floor(diff_seconds / 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Models with validation
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: EmailStr
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        import re
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        import re
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shift_not_equal(cls, v: str, info) -> str:
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("punched_at out of valid range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("punched_at out of valid range")
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_ms(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("out of valid range")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason length must be 5-200")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_by(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by length must be 1-50")
        return v


# Response models (for serialization to epoch ms)
class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int

    class Config:
        from_attributes = True


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[dict] = []


class PageOut(BaseModel):
    items: List[dict]
    total: int
    page: int
    page_size: int


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
def _index_exists(coll, name: str) -> bool:
    return name in coll.index_information()


@app.on_event("startup")
def create_indexes():
    # employees: unique emp_code, index on department, joined_on for analytics
    if not _index_exists(db.employees, "emp_code_1"):
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_1")
    if not _index_exists(db.employees, "department_1"):
        db.employees.create_index([("department", ASCENDING)], name="department_1")
    if not _index_exists(db.employees, "joined_on_1"):
        db.employees.create_index([("joined_on", ASCENDING)], name="joined_on_1")

    # attendance_logs: unique (emp_code, date) for race-safe punch-in
    if not _index_exists(db.attendance_logs, "emp_code_1_date_1"):
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_1_date_1")
    # For list_attendance: date desc + emp_code asc sort
    if not _index_exists(db.attendance_logs, "date_-1_emp_code_1"):
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_-1_emp_code_1")
    # For status filter + date sort
    if not _index_exists(db.attendance_logs, "status_1_date_-1"):
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="status_1_date_-1")
    # For analytics month range queries
    if not _index_exists(db.attendance_logs, "date_1"):
        db.attendance_logs.create_index([("date", ASCENDING)], name="date_1")


# --------------------------------------------------------------------------- #
# Helper: serialize attendance doc to API format (epoch ms)
# --------------------------------------------------------------------------- #
def serialize_attendance(doc: dict) -> dict:
    out = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for h in doc.get("history", []):
        changes = {}
        for field, change in h.get("changes", {}).items():
            # punch_in/punch_out values in history are already stored as epoch ms (or None)
            changes[field] = {
                "from": change.get("from"),
                "to": change.get("to"),
            }
        out["history"].append({
            "at": dt_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(UTC)
    punched_at = truncate_to_seconds(punched_at)

    date_str = compute_attendance_date(punched_at, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-out endpoint
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(UTC)
    punched_at = truncate_to_seconds(punched_at)

    # Find the most recent record with punch_in <= punched_at
    cursor = db.attendance_logs.find(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        {"_id": 0}
    ).sort("punch_in", DESCENDING).limit(1)

    record = next(cursor, None)
    if not record:
        # Check if there's already a punched-out record for today (or most recent)
        recent = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_out": {"$ne": None}},
            sort=[("punch_in", DESCENDING)]
        )
        if recent:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no open punch-in found for this employee")

    punch_in = record["punch_in"]
    if punched_at <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punched_at - punch_in).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    date_str = record["date"]
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # Compute derived fields
    work_hours = compute_work_hours(punch_in, punched_at)
    overtime_minutes = compute_overtime(punched_at, shift_end, date_str, shift_start)
    half_day = compute_half_day(work_hours)

    # Atomic update with version check (optimistic locking via punch_out being null)
    result = db.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": date_str, "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }}
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")

    # Return updated record
    updated = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": date_str}, {"_id": 0})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date} - Regularize
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = None,
):
    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Find record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    # Validate request body
    if not body:
        raise HTTPException(422, "request body required")

    new_status = record["status"]
    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    # Determine effective status (what the record will become)
    if body.status is not None:
        effective_status = body.status
    else:
        effective_status = record["status"]

    # Validate status vs punch times consistency
    if effective_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot set punch times with ABSENT/LEAVE status")
        new_status = body.status
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status: punch_in required
        if body.punch_in is not None:
            new_punch_in = ms_to_dt(body.punch_in)
            new_punch_in = truncate_to_seconds(new_punch_in)
            # Validate punch_in stays on record's attendance date (R1)
            if ist_date_from_dt(new_punch_in) != date:
                raise HTTPException(422, "punch_in must stay on record's attendance date")
        elif record.get("punch_in") is not None:
            new_punch_in = record["punch_in"]
        else:
            raise HTTPException(422, "presence status requires punch_in")
        if body.status is not None:
            new_status = body.status

    if body.punch_out is not None:
        new_punch_out = ms_to_dt(body.punch_out)
        new_punch_out = truncate_to_seconds(new_punch_out)
        if new_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    # Check if anything actually changed
    status_changed = body.status is not None and body.status != record["status"]

    if body.punch_in is not None:
        punch_in_changed = dt_to_ms(new_punch_in) != dt_to_ms(record.get("punch_in"))
    else:
        punch_in_changed = False

    if body.punch_out is not None:
        punch_out_changed = dt_to_ms(new_punch_out) != dt_to_ms(record.get("punch_out"))
    else:
        punch_out_changed = False

    if not (status_changed or punch_in_changed or punch_out_changed):
        raise HTTPException(422, "no changes made")

    # Compute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"])
        if new_punch_out is not None:
            work_hours = compute_work_hours(new_punch_in, new_punch_out)
            overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            half_day = compute_half_day(work_hours)
        else:
            work_hours = None
            overtime_minutes = 0
            half_day = False

    # Build history entry
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {},
    }

    if status_changed:
        history_entry["changes"]["status"] = {"from": record["status"], "to": new_status}
    if punch_in_changed:
        history_entry["changes"]["punch_in"] = {
            "from": dt_to_ms(record["punch_in"]) if record.get("punch_in") else None,
            "to": dt_to_ms(new_punch_in) if new_punch_in else None,
        }
    if punch_out_changed:
        history_entry["changes"]["punch_out"] = {
            "from": dt_to_ms(record["punch_out"]) if record.get("punch_out") else None,
            "to": dt_to_ms(new_punch_out) if new_punch_out else None,
        }

    # Derived field changes
    if record.get("late_minutes", 0) != late_minutes:
        history_entry["changes"]["late_minutes"] = {"from": record.get("late_minutes", 0), "to": late_minutes}
    if record.get("work_hours") != work_hours:
        history_entry["changes"]["work_hours"] = {"from": record.get("work_hours"), "to": work_hours}
    if record.get("overtime_minutes", 0) != overtime_minutes:
        history_entry["changes"]["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": overtime_minutes}
    if record.get("half_day", False) != half_day:
        history_entry["changes"]["half_day"] = {"from": record.get("half_day", False), "to": half_day}

    # Atomic update with optimistic locking (version = history length)
    history_len = len(record.get("history", []))
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        },
        "$push": {"history": history_entry},
    }
    result = db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": date, "history": {"$size": history_len}},
        update_doc
    )
    if result.matched_count == 0:
        raise HTTPException(409, "concurrent modification, please retry")

    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #

def _month_bounds(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) for a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    first = f"{year:04d}-{mon:02d}-01"
    if mon == 12:
        last = f"{year+1:04d}-01-01"
    else:
        last = f"{year:04d}-{mon+1:02d}-01"
    # last_day is the day before next month start
    from datetime import date, timedelta
    last_dt = date.fromisoformat(last) - timedelta(days=1)
    return first, last_dt.isoformat()


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    month_start, month_end = _month_bounds(month)
    joined_on = emp["joined_on"]

    # Working days: Mon-Fri from joined_on to month_end
    from datetime import date, timedelta
    start_dt = max(date.fromisoformat(month_start), date.fromisoformat(joined_on))
    end_dt = date.fromisoformat(month_end)
    working_days = 0
    cur = start_dt
    while cur <= end_dt:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)

    # Aggregate attendance logs for this employee in this month
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$in": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}, "timezone": "+05:30"}}, [2,3,4,5,6]]}
                        ]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    agg = result[0] if result else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0
    }

    present_days = agg["present_days"]
    attendance_pct = None
    if working_days > 0:
        # Half-up rounding for 2 decimals (R8)
        raw = present_days / working_days * 100
        attendance_pct = math.floor(raw * 100 + 0.5) / 100

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg["leave_days"],
        "late_count": agg["late_count"],
        "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = _month_bounds(month)

    # Get all departments (or filtered)
    dept_q = {}
    if department:
        dept_q["department"] = department
    departments = list(db.employees.find(dept_q, {"_id": 0, "department": 1}).distinct("department"))

    items = []
    for dept in sorted(departments):
        # Headcount: employees in dept with joined_on <= month_end
        headcount = db.employees.count_documents({
            "department": dept,
            "joined_on": {"$lte": month_end}
        })
        if headcount == 0:
            continue

        # Aggregate logs for this department in this month
        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {
                "emp.department": dept,
                "emp.joined_on": {"$lte": month_end}
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}, "timezone": "+05:30"}}, [2,3,4,5,6]]}
                            ]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "avg_work_hours": {"$avg": "$work_hours"},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }}
        ]
        result = list(db.attendance_logs.aggregate(pipeline))
        agg = result[0] if result else {
            "present_days": 0, "avg_work_hours": None, "late_count": 0,
            "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0
        }

        avg_wh = agg["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = math.floor(avg_wh * 100 + 0.5) / 100

        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": agg["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": agg["late_count"],
            "total_late_minutes": agg["total_late_minutes"],
            "leave_count": agg["leave_count"],
            "on_duty_count": agg["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = _month_bounds(month)

    # Build match conditions
    match_conditions = {"date": {"$gte": month_start, "$lte": month_end}}
    if department:
        match_conditions["emp.department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {k: v for k, v in match_conditions.items() if k != "date"}},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ]
    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if from_date > to_date:
        raise HTTPException(422, "from must be <= to")
    from datetime import date, timedelta
    if (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    # Build IST-midnight datetimes for $densify bounds (literal dates required)
    from datetime import datetime as _dt
    bound_start = _dt.fromisoformat(from_date).replace(tzinfo=IST)
    bound_end = _dt.fromisoformat(to_date).replace(tzinfo=IST)

    # Use $densify with bounds to fill gaps
    pipeline = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {
            "emp.department": department
        }},
        {"$group": {
            "_id": {"date": "$date"},
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        ]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$addFields": {
            "day": {"$dateFromString": {"dateString": "$_id.date", "timezone": "+05:30"}}
        }},
        {"$densify": {
            "field": "day",
            "range": {
                "bounds": [bound_start, bound_end],
                "step": 1,
                "unit": "day"
            }
        }},
        {"$sort": {"day": 1}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$present_count",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"dept": department, "day": "$day"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$eq": ["$department", "$$dept"]},
                        {"$lte": ["$joined_on", {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d", "timezone": "+05:30"}}]}
                    ]}
                }},
                {"$count": "headcount"}
            ],
            "as": "headcount_result"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.headcount", 0]}, 0]},
            "is_working_day": {"$in": [{"$isoDayOfWeek": {"date": "$day", "timezone": "+05:30"}}, [2,3,4,5,6]]},
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": [{"$ifNull": ["$present_count", 0]}, "$headcount"]}, 4]},
                    None
                ]
            }
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "+05:30"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }}
    ]
    results = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": results}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Build the same pipeline/query as the real endpoint
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        coll = db.attendance_logs
        pipeline = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        month_start, month_end = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": None}},
        ]
        collection_name = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        month_start, month_end = _month_bounds(month)
        if department:
            dept_exists = db.employees.find_one({"department": department})
            if not dept_exists:
                raise HTTPException(404, "department not found")
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.joined_on": {"$lte": month_end}, **({"emp.department": department} if department else {})}},
            {"$group": {"_id": None}},
        ]
        collection_name = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        month_start, month_end = _month_bounds(month)
        post_lookup_match = {"emp.department": department} if department else {}
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": post_lookup_match},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
        ]
        collection_name = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, to required")
        if from_date > to_date:
            raise HTTPException(422, "from must be <= to")
        from datetime import date as dt_date, datetime as _dt
        if (dt_date.fromisoformat(to_date) - dt_date.fromisoformat(from_date)).days > 92:
            raise HTTPException(422, "range exceeds 92 days")
        bound_start = _dt.fromisoformat(from_date).replace(tzinfo=IST)
        bound_end = _dt.fromisoformat(to_date).replace(tzinfo=IST)
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": {"date": "$date"}}},
            {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id.date", "timezone": "+05:30"}}}},
            {"$densify": {"field": "day", "range": {"bounds": [bound_start, bound_end], "step": 1, "unit": "day"}}},
        ]
        collection_name = "attendance_logs"
    else:
        raise HTTPException(422, "unknown endpoint")

    # Run explain with executionStats
    explain_result = db.command(
        "explain",
        {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats"
    )

    # Convert BSON types (Timestamp, ObjectId, etc.) to JSON-safe structures
    import json as _json
    explain_json = _json.loads(json_util.dumps(explain_result))

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_json
    }