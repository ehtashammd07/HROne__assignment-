"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
import json
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load local .env from project root if present (do not search parent directories)
repo_dotenv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(repo_dotenv):
    load_dotenv(dotenv_path=repo_dotenv)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    except Exception:
        # Ignore startup index errors if DB is unreachable during build/lint steps
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int) -> Optional[float]:
    """R8: Rounding of reported numbers half-up (rates: 4 decimals, numbers: 2 decimals)."""
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def truncate_epoch_ms(ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC timezone-aware datetime."""
    truncated_ms = truncate_epoch_ms(ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        eh, em = map(int, shift_end.split(":"))
        shift_end_time = time(eh, em)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, date_str: str, shift_start: str) -> int:
    """
    R2: Late.
    Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start (not grace end). Otherwise 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    d = date.fromisoformat(date_str)
    shift_start_ist = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = diff_sec / 3600.0
    return round_half_up(hours, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day = true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime_minutes(punch_out_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    d = date.fromisoformat(date_str)
    if shift_end <= shift_start:
        d = d + timedelta(days=1)
    shift_end_ist = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    ot_minutes = int(diff_sec // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days. Mon-Fri, counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > last_day:
        return 0

    start_date = max(first_day, joined_date)
    working_days = 0
    curr = start_date
    while curr <= last_day:
        if curr.weekday() < 5:
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            from_val = datetime_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from")
            to_val = datetime_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to")
            changes_api[k] = {"from": from_val, "to": to_val}
        else:
            changes_api[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": datetime_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_api
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])]
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_difference(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_has_change_field(self):
        if self.status is None and self.punch_in is None and self.punch_out is None:
            raise ValueError("At least one change field (status, punch_in, punch_out) must be provided")
        return self


# --------------------------------------------------------------------------- #
# Pipeline Builders (Used by Endpoints and Explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    match_emp = {"joined_on": {"$lte": f"{month}-31"}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code"
                },
                "department": {"$first": "$department"},
                "logs": {"$push": "$logs"}
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$_id.emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt_plus_1 = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
                "date_val": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "date_val": {"$literal": from_dt}}}
                ]
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1]
                }
            }
        },
        {
            "$group": {
                "_id": "$date_val",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_val": "$_id",
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {"$round": ["$moving_avg_raw", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces unique emp_code via database unique index.
    Created_at set by server as BSON datetime (returned as epoch ms).
    """
    now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = [serialize_employee(emp) for emp in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Creates the day's record (R1, R2). Race-safe against simultaneous punch-ins for the same employee and date.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
        punch_in_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work hours, overtime, half day (R3-R5). Works across midnight.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
        punch_out_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    # Find the most recent record with punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    in_ms = int(punch_in_utc.timestamp() * 1000)
    out_ms = int(punch_out_utc.timestamp() * 1000)

    if out_ms <= in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")
    if out_ms - in_ms > 24 * 3600 * 1000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    ot_min = compute_overtime_minutes(punch_out_utc, rec["date"], emp["shift_start"], emp["shift_end"])

    # Atomic update to avoid race conditions between concurrent punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": ot_min,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Indexed and fast at 100k docs.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_attendance(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...
):
    """
    Regularize (manually correct) one day's record:
    Recompute derived fields, append to history, race-safe.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    old_status = rec["status"]
    old_in = rec.get("punch_in")
    if old_in is not None and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=timezone.utc)
    old_out = rec.get("punch_out")
    if old_out is not None and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=timezone.utc)
    old_work_hours = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_half_day = bool(rec.get("half_day", False))

    final_status = body.status if body.status is not None else old_status

    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot provide punch times when setting status to ABSENT or LEAVE")
        final_in = None
        final_out = None
    elif final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot provide punch times for an ABSENT or LEAVE record without changing status")
        final_in = None
        final_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            final_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            final_in = old_in

        if final_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires a punch_in")

        # R1 attendance date check
        calc_date = get_attendance_date(final_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_param:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"punch_in belongs to attendance date {calc_date}, not {date_param}")

        if body.punch_out is not None:
            final_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            final_out = old_out

        if final_out is not None:
            in_ms = int(final_in.timestamp() * 1000)
            out_ms = int(final_out.timestamp() * 1000)
            if out_ms <= in_ms:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")
            if out_ms - in_ms > 24 * 3600 * 1000:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if final_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late = 0
        new_ot = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(final_in, date_param, emp["shift_start"])
        if final_out is not None:
            new_work_hours = compute_work_hours(final_in, final_out)
            new_half_day = compute_half_day(new_work_hours)
            new_ot = compute_overtime_minutes(final_out, date_param, emp["shift_start"], emp["shift_end"])
        else:
            new_work_hours = None
            new_half_day = False
            new_ot = 0

    # Determine changes
    changes_mongo = {}
    if final_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": final_status}

    def datetimes_equal(a: Optional[datetime], b: Optional[datetime]) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return int(a.timestamp() * 1000) == int(b.timestamp() * 1000)

    if not datetimes_equal(final_in, old_in):
        changes_mongo["punch_in"] = {"from": old_in, "to": final_in}

    if not datetimes_equal(final_out, old_out):
        changes_mongo["punch_out"] = {"from": old_out, "to": final_out}

    if new_work_hours != old_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if new_late != old_late:
        changes_mongo["late_minutes"] = {"from": old_late, "to": new_late}

    if new_ot != old_ot:
        changes_mongo["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    if new_half_day != old_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes detected")

    now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo
    }

    current_history = rec.get("history", [])

    # Optimistic locking on history to avoid losing concurrent history entries
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": current_history},
        {
            "$set": {
                "status": final_status,
                "punch_in": final_in,
                "punch_out": final_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent modification conflict")

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late = int(data.get("total_late_minutes", 0))
        total_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    """Department summary for a month across both collections."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0))
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    """Late-comers leaderboard for a month with standard competition ranking."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"])
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    """Daily attendance trend for a department with gap filling and 7-day moving average."""
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if to_date < from_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        rate = r.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)
        mavg = r.get("moving_avg_7d")
        if mavg is not None:
            mavg = round_half_up(mavg, 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r.get("present_count", 0.0)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": mavg
        })

    return {
        "department": department,
        "items": items
    }


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """MongoDB explain output for an endpoint's main query with executionStats verbosity."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        coll_name = "attendance_logs"
        cmd = {
            "explain": {
                "find": coll_name,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        coll_name = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": coll_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        coll_name = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": coll_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        coll_name = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {
                "aggregate": coll_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")
        if to_date < from_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range exceeds 92 days")

        coll_name = "attendance_logs"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {
                "aggregate": coll_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)

    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid endpoint")

    clean_explain = json.loads(json_util.dumps(res))
    return {
        "endpoint": endpoint,
        "collection": coll_name,
        "explain": clean_explain
    }
