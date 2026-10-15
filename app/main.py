"""
Employee Attendance & Analytics API - HROne Assignment

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file implements all endpoints according to the OpenAPI contract and business rules R1-R10.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, TEXT
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers - Rounding, Timezone, and Calculations
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    """Round using half-up method (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    quantize_str = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def parse_time(time_str: str) -> tuple[int, int]:
    """Parse HH:MM time string to (hour, minute)."""
    h, m = map(int, time_str.split(":"))
    return h, m


def compute_ist_datetime_from_epoch(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to IST datetime."""
    # Convert to seconds, create UTC datetime, then convert to IST
    ts_seconds = epoch_ms / 1000.0
    dt_utc = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
    return dt_utc.astimezone(IST)


def compute_ist_date_from_epoch(epoch_ms: int) -> str:
    """Get the IST calendar date (YYYY-MM-DD) from an epoch milliseconds."""
    dt_ist = compute_ist_datetime_from_epoch(epoch_ms)
    return dt_ist.strftime("%Y-%m-%d")


def format_datetime_epoch(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift is overnight (end <= start)."""
    start_h, start_m = parse_time(shift_start)
    end_h, end_m = parse_time(shift_end)
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    return end_total <= start_total


def get_shift_datetime(date_str: str, shift_time: str, shift_is_overnight: bool) -> datetime:
    """Get the shift start/end datetime for a given date."""
    h, m = parse_time(shift_time)
    date_parts = date_str.split("-")
    year, month, day = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
    
    dt = datetime(year, month, day, h, m, tzinfo=IST)
    
    # For overnight shifts, the shift_end is on the next day
    if shift_is_overnight:
        dt = dt + timedelta(days=1)
    
    return dt


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, shift_is_overnight: bool) -> int:
    """Calculate late minutes per R2: 10-min grace, floor minutes after grace."""
    shift_start_dt = get_shift_datetime(punch_in_dt.strftime("%Y-%m-%d"), shift_start, shift_is_overnight)
    
    punch_min = punch_in_dt.hour * 60 + punch_in_dt.minute
    shift_min = shift_start_dt.hour * 60 + shift_start_dt.minute
    
    diff = punch_min - shift_min
    # Grace period: only late if strictly more than 10 minutes
    if diff <= 10:
        return 0
    return diff // 1  # floor


def compute_overtime(punch_out_dt: datetime, shift_end: str, date_str: str, shift_is_overnight: bool) -> int:
    """Calculate overtime minutes per R3: floor minutes, only when >= 30."""
    shift_end_dt = get_shift_datetime(date_str, shift_end, shift_is_overnight)
    
    punch_min = punch_out_dt.hour * 60 + punch_out_dt.minute
    shift_min = shift_end_dt.hour * 60 + shift_end_dt.minute
    
    diff = punch_min - shift_min
    if diff < 30:
        return 0
    return diff // 1  # floor


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """Calculate work hours per R4: seconds/3600, half-up rounded to 2 decimals."""
    duration = punch_out_dt - punch_in_dt
    hours = duration.total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_half_day(work_hours: float) -> bool:
    """Determine half day per R5: true if rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def is_weekday(dt: datetime) -> bool:
    """Check if date is Monday-Friday."""
    return dt.weekday() < 5  # 0=Mon, 6=Sun


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Startup - Create Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def create_indexes():
    """Create MongoDB indexes at startup."""
    # Employees: unique on emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    
    # Attendance: compound for common queries
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING)])
    
    print("MongoDB indexes created successfully.")


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def employee_to_dict(emp: dict) -> dict:
    """Convert employee doc to response dict, removing _id and converting created_at."""
    if emp is None:
        return None
    result = {k: v for k, v in emp.items() if k != "_id"}
    if "created_at" in result and isinstance(result["created_at"], datetime):
        result["created_at"] = format_datetime_epoch(result["created_at"])
    return result


def attendance_to_dict(record: dict) -> dict:
    """Convert attendance doc to response dict, removing _id and converting timestamps."""
    if record is None:
        return None
    result = {}
    for k, v in record.items():
        if k == "_id":
            continue
        if k in ("punch_in", "punch_out", "history.at"):
            if isinstance(v, datetime):
                result[k] = format_datetime_epoch(v)
            else:
                result[k] = v
        elif k == "history":
            # Convert history array
            history = []
            for entry in v:
                hist_entry = {
                    "at": format_datetime_epoch(entry["at"]) if isinstance(entry["at"], datetime) else entry["at"],
                    "by": entry["by"],
                    "reason": entry["reason"],
                    "changes": entry.get("changes", {})
                }
                # Convert punch times in changes
                for field in ("punch_in", "punch_out"):
                    if field in hist_entry["changes"]:
                        old_val = hist_entry["changes"][field].get("from")
                        new_val = hist_entry["changes"][field].get("to")
                        if isinstance(old_val, datetime):
                            hist_entry["changes"][field]["from"] = format_datetime_epoch(old_val)
                        if isinstance(new_val, datetime):
                            hist_entry["changes"][field]["to"] = format_datetime_epoch(new_val)
                history.append(hist_entry)
            result[k] = history
        else:
            result[k] = v
    return result


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe - returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code enforcement."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).astimezone(IST)
    
    try:
        db.employees.insert_one(doc)
        doc.pop("_id", None)
        return doc
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    """List employees with proper pagination and filtered total count."""
    q = {}
    if department:
        q["department"] = department
    
    # Count filtered results
    total = db.employees.count_documents(q)
    
    # Pagination: (page - 1) * page_size, max page_size = 100
    skip = (page - 1) * page_size
    
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size).sort("emp_code", ASCENDING))
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in with race condition protection and IST handling."""
    # Validate status
    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(status_code=422, detail="Invalid status for punch-in")
    
    # Find employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Calculate attendance date (R1 - IST timezone)
    if body.punched_at is None:
        punched_at_dt = datetime.now(timezone.utc).astimezone(IST)
        punched_at_ms = format_datetime_epoch(punched_at_dt)
    else:
        punched_at_dt = compute_ist_datetime_from_epoch(body.punched_at)
        punched_at_ms = body.punched_at
    
    date_str = compute_ist_date_from_epoch(punched_at_ms)
    shift_is_overnight = is_overnight_shift(emp["shift_start"], emp["shift_end"])
    
    # Check if already punched in (atomic check-and-insert with unique index)
    existing = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": date_str
    })
    if existing:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    
    # Calculate late minutes (R2)
    late_minutes = compute_late_minutes(punched_at_dt, emp["shift_start"], shift_is_overnight)
    
    # Create record with atomic insert
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    
    try:
        db.attendance_logs.insert_one(doc)
        record = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": date_str})
        return attendance_to_dict(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out with validation and derived field computation."""
    # Find employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    shift_is_overnight = is_overnight_shift(shift_start, shift_end)
    
    # Calculate punch time
    if body.punched_at is None:
        punched_at_dt = datetime.now(timezone.utc).astimezone(IST)
        punched_at_ms = format_datetime_epoch(punched_at_dt)
    else:
        punched_at_dt = compute_ist_datetime_from_epoch(body.punched_at)
        punched_at_ms = body.punched_at
    
    # Find the record (most recent with punch_in <= punched_at)
    # For overnight shifts, we look for records on the current or previous date
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        "punch_in": {"$lte": punched_at_dt},
        "punch_out": None
    })
    
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found to punch out")
    
    # Validate punch_out is after punch_in
    punch_in_dt = record["punch_in"]
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    
    # Validate within 24 hours
    max_punch_out = punch_in_dt + timedelta(hours=24)
    if punched_at_dt > max_punch_out:
        raise HTTPException(status_code=422, detail="punch_out more than 24 hours after punch_in")
    
    # Compute derived fields (R3-R5)
    work_hours = compute_work_hours(punch_in_dt, punched_at_dt)
    overtime_minutes = compute_overtime(punched_at_dt, shift_end, record["date"], shift_is_overnight)
    half_day = compute_half_day(work_hours)
    
    # Update the record atomically
    update_doc = {
        "punch_out": punched_at_dt,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day
    }
    
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {"$set": update_doc},
        return_document=True
    )
    
    return attendance_to_dict(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    """List attendance records with proper filtering, sorting, and pagination."""
    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    
    # Date range filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    
    # Count total with filters
    total = db.attendance_logs.count_documents(q)
    
    # Pagination
    skip = (page - 1) * page_size
    
    # Sort by date desc, emp_code asc
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .skip(skip)
        .limit(page_size)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    )
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct an attendance record with audit trail."""
    # Validate reason length
    if len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(status_code=422, detail="Reason must be 5-200 characters")
    
    if len(body.regularized_by) < 1 or len(body.regularized_by) > 50:
        raise HTTPException(status_code=422, detail="regularized_by must be 1-50 characters")
    
    # Find employee
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Find record
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    # Build update and track changes
    update_doc = {}
    history_entry = {
        "at": datetime.now(timezone.utc).astimezone(IST),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {}
    }
    
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    shift_is_overnight = is_overnight_shift(shift_start, shift_end)
    
    # Track original values
    original = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False)
    }
    
    # Validate and apply status change
    if body.status is not None:
        if body.status in ("ABSENT", "LEAVE"):
            # Clear punch times for ABSENT/LEAVE
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="Cannot set punch_in or punch_out for ABSENT or LEAVE status")
            update_doc["punch_in"] = None
            update_doc["punch_out"] = None
        else:
            # Presence status requires punch_in
            if body.punch_in is None:
                if record.get("punch_in") is None:
                    raise HTTPException(status_code=422, detail="Presence status requires a punch_in time")
            else:
                # Validate punch_in is on the correct date (R1)
                punch_in_dt = compute_ist_datetime_from_epoch(body.punch_in)
                expected_date = compute_ist_date_from_epoch(body.punch_in)
                if expected_date != date:
                    raise HTTPException(status_code=422, detail=f"punch_in must be on attendance date {date}")
                update_doc["punch_in"] = punch_in_dt
        
        update_doc["status"] = body.status
    
    # Validate and apply punch_in change
    if body.punch_in is not None:
        # Only set if not already set by status change
        if "punch_in" not in update_doc:
            punch_in_dt = compute_ist_datetime_from_epoch(body.punch_in)
            update_doc["punch_in"] = punch_in_dt
    
    # Validate and apply punch_out change
    if body.punch_out is not None:
        punch_out_dt = compute_ist_datetime_from_epoch(body.punch_out)
        
        # Get punch_in time for validation
        punch_in_dt = update_doc.get("punch_in") or record.get("punch_in")
        if punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Cannot punch out when punch_in is null")
        
        if punch_out_dt <= punch_in_dt:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        
        max_punch_out = punch_in_dt + timedelta(hours=24)
        if punch_out_dt > max_punch_out:
            raise HTTPException(status_code=422, detail="punch_out more than 24 hours after punch_in")
        
        update_doc["punch_out"] = punch_out_dt
    
    # Compute derived fields after changes
    if update_doc.get("punch_in") is not None:
        punch_in_dt = update_doc["punch_in"]
        late_minutes = compute_late_minutes(punch_in_dt, shift_start, shift_is_overnight)
        update_doc["late_minutes"] = late_minutes
        
        if update_doc.get("punch_out") is not None:
            punch_out_dt = update_doc["punch_out"]
            work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
            overtime_minutes = compute_overtime(punch_out_dt, shift_end, date, shift_is_overnight)
            half_day = compute_half_day(work_hours)
            
            update_doc["work_hours"] = work_hours
            update_doc["overtime_minutes"] = overtime_minutes
            update_doc["half_day"] = half_day
    elif update_doc.get("punch_out") is not None:
        raise HTTPException(status_code=422, detail="Cannot punch out without a punch_in time")
    
    # Check if anything changed
    if not update_doc:
        raise HTTPException(status_code=422, detail="No fields changed")
    
    # Track changes for history
    for key, new_val in update_doc.items():
        if key in original and original[key] != new_val:
            old_val = original[key]
            history_entry["changes"][key] = {
                "from": old_val,
                "to": new_val
            }
    
    # Update record
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {"$set": update_doc, "$push": {"history": history_entry}},
        return_document=True
    )
    
    return attendance_to_dict(result)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    """Monthly attendance summary for one employee using MongoDB aggregation."""
    # Validate month format
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format (expected YYYY-MM)")
    
    # Find employee
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Parse month
    year, month_num = int(month[:4]), int(month[5:7])
    
    # Get first and last day of month
    first_day = f"{year:04d}-{month_num:02d}-01"
    if month_num == 12:
        last_day = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Calculate working days (R7 - Mon-Fri from joined_on to end of month)
    joined_on = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    end_of_month = datetime.strptime(last_day, "%Y-%m-%d").date()
    
    working_days = 0
    current = max(joined_on, datetime(year, month_num, 1).date())
    while current <= end_of_month:
        if current.isoweekday() < 6:  # Mon-Fri
            working_days += 1
        current += timedelta(days=1)
    
    # Aggregation for attendance stats
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {
                            "$cond": [
                                {"$eq": ["$half_day", True]},
                                0.5,
                                1.0
                            ]
                        },
                        0
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]
    
    result = list(db.attendance_logs.aggregate(pipeline))
    
    if result:
        stats = result[0]
        present_days = stats.get("present_days", 0)
        leave_days = stats.get("leave_days", 0)
        late_count = stats.get("late_count", 0)
        total_late_minutes = stats.get("total_late_minutes", 0)
        total_overtime_minutes = stats.get("total_overtime_minutes", 0)
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0
    
    # Calculate attendance percentage (R8)
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 4)
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    """Department summary for a month using MongoDB aggregation."""
    # Validate month format
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format (expected YYYY-MM)")
    
    # Parse month
    year, month_num = int(month[:4]), int(month[5:7])
    
    # Get last day of month
    if month_num == 12:
        last_day_str = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day_str = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Build pipeline
    pipeline = []
    
    # Join employees with attendance
    pipeline.extend([
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f"{year:04d}-{month_num:02d}-01", "$lte": last_day_str}}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}
    ])
    
    # Filter by department if specified
    if department:
        pipeline.append({"$match": {"department": department}})
    
    # Group by department
    group_stage = {
        "_id": "$department",
        "headcount": {
            "$sum": {
                "$cond": [
                    {"$lte": ["$joined_on", last_day_str]},
                    1,
                    0
                ]
            }
        },
        "present_days": {
            "$sum": {
                "$cond": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {
                        "$cond": [
                            {"$eq": ["$logs.half_day", True]},
                            0.5,
                            1.0
                        ]
                    },
                    0
                ]
            }
        },
        "work_hours_sum": {
            "$sum": {
                "$cond": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ifNull": ["$logs.work_hours", 0]},
                    0
                ]
            }
        },
        "work_hours_count": {
            "$sum": {
                "$cond": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ne": ["$logs.work_hours", None]}, 1, 0]},
                    0
                ]
            }
        },
        "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": "$logs.late_minutes"},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
    }
    
    pipeline.append({"$group": group_stage})
    pipeline.append({"$match": {"headcount": {"$gt": 0}}})  # Omit departments with no headcount
    pipeline.append({"$sort": {"_id": ASCENDING}})
    
    results = list(db.employees.aggregate(pipeline))
    
    items = []
    for r in results:
        avg_work_hours = None
        if r["work_hours_count"] > 0:
            avg_work_hours = round_half_up(r["work_hours_sum"] / r["work_hours_count"], 2)
        
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })
    
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    """Late-comers leaderboard with competition ranking."""
    # Validate month format
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format (expected YYYY-MM)")
    
    # Parse month
    year, month_num = int(month[:4]), int(month[5:7])
    
    # Get last day of month
    if month_num == 12:
        last_day_str = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day_str = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Build pipeline
    pipeline = [
        {"$match": {"date": {"$gte": f"{year:04d}-{month_num:02d}-01", "$lte": last_day_str}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},  # Only employees with late minutes
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"}
    ]
    
    # Filter by department if specified
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    
    pipeline.extend([
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])
    
    results = list(db.attendance_logs.aggregate(pipeline))
    
    # Assign competition ranks (ties share rank, next rank skipped)
    items = []
    current_rank = 1
    prev_total = None
    
    for i, r in enumerate(results):
        if prev_total is not None and r["total_late_minutes"] < prev_total:
            current_rank = i + 1
        
        items.append({
            "rank": current_rank,
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"]
        })
        
        prev_total = r["total_late_minutes"]
    
    # Filter by rank <= limit (after ranking)
    final_items = [item for item in items if item["rank"] <= limit]
    
    return {
        "month": month,
        "items": final_items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to_date: str):
    """Daily attendance trend for a department with gap-filling."""
    # Validate date range
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date cannot be after to_date")
    
    # Check date range is not more than 92 days
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    if (end - start).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    
    # Find department employees
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    
    # Generate date range
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    
    # Get employee count per day
    headcount_by_date = {}
    for date in dates:
        count = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": date}
        })
        headcount_by_date[date] = count
    
    # Get attendance stats per day
    stats_pipeline = [
        {"$match": {
            "emp_code": {"$in": list(db.employees.distinct("emp_code", {"department": department}))},
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {
                            "$cond": [
                                {"$eq": ["$half_day", True]},
                                0.5,
                                1.0
                            ]
                        },
                        0
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }}
    ]
    
    stats = {}
    for r in db.attendance_logs.aggregate(stats_pipeline):
        stats[r["_id"]] = {
            "present_count": r["present_count"],
            "late_count": r["late_count"]
        }
    
    # Build trend with gap-filling
    items = []
    for date in dates:
        is_working_day = datetime.strptime(date, "%Y-%m-%d").isoweekday() < 6
        headcount = headcount_by_date[date]
        present_count = stats.get(date, {}).get("present_count", 0)
        late_count = stats.get(date, {}).get("late_count", 0)
        
        # Calculate attendance rate
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        
        items.append({
            "date": date,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None  # Will be calculated next
        })
    
    # Calculate 7-day moving average
    for i in range(len(items)):
        window = items[max(0, i-6):i+1]
        rates = [item["attendance_rate"] for item in window if item["attendance_rate"] is not None]
        if rates:
            items[i]["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4)
    
    return {
        "department": department,
        "items": items
    }


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    """MongoDB explain for an endpoint's main query."""
    # Validate required parameters
    if endpoint == "employee_monthly":
        if not month:
            raise HTTPException(status_code=422, detail="month parameter is required")
        return explain_employee_monthly(month)
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month parameter is required")
        return explain_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month parameter is required")
        return explain_late_leaderboard(month, department)
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to parameters are required")
        return explain_department_trend(department, from_date, to_date)
    elif endpoint == "attendance_list":
        return explain_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")


def explain_employee_monthly(month: str):
    """Explain employee monthly query."""
    year, month_num = int(month[:4]), int(month[5:7])
    
    if month_num == 12:
        last_day = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    pipeline = [
        {"$match": {"date": {"$gte": f"{year:04d}-{month_num:02d}-01", "$lte": last_day}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }}
    ]
    
    explain = db.attendance_logs.explain("aggregate", pipeline, verbosity="executionStats")
    
    return {
        "endpoint": "employee_monthly",
        "collection": "attendance_logs",
        "explain": explain
    }


def explain_department_summary(month: str, department: Optional[str] = None):
    """Explain department summary query."""
    year, month_num = int(month[:4]), int(month[5:7])
    
    if month_num == 12:
        last_day_str = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day_str = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    pipeline = [
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f"{year:04d}-{month_num:02d}-01", "$lte": last_day_str}}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}
    ]
    
    if department:
        pipeline.append({"$match": {"department": department}})
    
    pipeline.append({
        "$group": {
            "_id": "$department",
            "headcount": {"$sum": 1}
        }
    })
    
    explain = db.employees.explain("aggregate", pipeline, verbosity="executionStats")
    
    return {
        "endpoint": "department_summary",
        "collection": "employees",
        "explain": explain
    }


def explain_late_leaderboard(month: str, department: Optional[str] = None):
    """Explain late leaderboard query."""
    year, month_num = int(month[:4]), int(month[5:7])
    
    if month_num == 12:
        last_day_str = f"{year:04d}-12-31"
    else:
        next_month = datetime(year, month_num + 1, 1) if month_num < 12 else datetime(year + 1, 1, 1)
        last_day_str = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    
    pipeline = [
        {"$match": {"date": {"$gte": f"{year:04d}-{month_num:02d}-01", "$lte": last_day_str}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING}}
    ]
    
    explain = db.attendance_logs.explain("aggregate", pipeline, verbosity="executionStats")
    
    return {
        "endpoint": "late_leaderboard",
        "collection": "attendance_logs",
        "explain": explain
    }


def explain_department_trend(department: str, from_date: str, to_date: str):
    """Explain department trend query."""
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": 1}
        }}
    ]
    
    if department:
        emp_codes = list(db.employees.distinct("emp_code", {"department": department}))
        pipeline[0]["$match"]["emp_code"] = {"$in": emp_codes}
    
    explain = db.attendance_logs.explain("aggregate", pipeline, verbosity="executionStats")
    
    return {
        "endpoint": "department_trend",
        "collection": "attendance_logs",
        "explain": explain
    }


def explain_attendance_list(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                           date_to: Optional[str] = None, status: Optional[str] = None,
                           page: int = 1, page_size: int = 20):
    """Explain attendance list query."""
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    
    # Build explain query
    explain = db.attendance_logs.explain(
        "find",
        q,
        sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
        skip=(page - 1) * page_size,
        limit=page_size,
        verbosity="executionStats"
    )
    
    return {
        "endpoint": "attendance_list",
        "collection": "attendance_logs",
        "explain": explain
    }


# --------------------------------------------------------------------------- #
# Error Handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(422)
async def validation_exception_handler(request, exc):
    """Custom validation error handler."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
