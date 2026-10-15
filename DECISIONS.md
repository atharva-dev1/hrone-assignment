# Design Decisions - HROne Employee Attendance & Analytics API

## 1. Validation Approach

### Request Validation (Pydantic Models)
- Used Pydantic for automatic request body validation
- Custom validators ensure email format, emp_code format (EMP#### to EMP######)
- Field-level constraints (length, pattern, enum values)

### Business Rule Validation
- **Status combinations**: LEAVE/ABSENT cannot have punch times
- **Time constraints**: punch_out > punch_in, within 24 hours
- **Date range validation**: date_from <= date_to, trend range <= 92 days
- **Manual validation**: reason length (5-200), regularized_by length (1-50)

## 2. Concurrency Strategy

### Race Condition Prevention
**Punch-in duplicates**: MongoDB unique index on `{emp_code: 1, date: 1}`
```python
db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)], 
    unique=True
)
```
- Database enforces uniqueness atomically
- DuplicateKeyError returns 409 Conflict
- No application-level check needed

**Punch-out duplicates**: `find_one_and_update()` with `$set`
- Atomic update operation
- Returns 409 if record already updated

**Regularization**: `find_one_and_update()` with `$set` and `$push`
- Single atomic operation
- Prevents lost updates

## 3. MongoDB Indexes

### Indexes Created at Startup
```python
# Employees: unique on emp_code
db.employees.create_index([("emp_code", ASCENDING)], unique=True)

# Attendance: compound for common queries
db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
db.attendance_logs.create_index([("emp_code", ASCENDING)])
```

### Index Justification
| Query Pattern | Index Used |
|---------------|------------|
| Employee lookup by emp_code | `employees(emp_code)` |
| Attendance by emp_code + date | `attendance_logs(emp_code, date)` |
| Attendance list with filters | `attendance_logs(date, emp_code)` |
| Aggregation groups by emp_code | `attendance_logs(emp_code)` |
| Sort by date desc, emp_code asc | `attendance_logs(date, emp_code)` |

## 4. Aggregation Pipeline Design

### Employee Monthly Summary
```python
pipeline = [
    {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
    {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [...]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        ...
    }}
]
```
- Filters by employee and date range first
- Uses conditional aggregation for partial day counting (0.5 for half_day)

### Department Summary (with $lookup)
```python
pipeline = [
    {"$lookup": {
        "from": "attendance_logs",
        "localField": "emp_code",
        "foreignField": "emp_code",
        "pipeline": [{"$match": {"date": {"$gte": ..., "$lte": ...}}}],
        "as": "logs"
    }},
    {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
    {"$group": {...}}
]
```
- Joins employees with attendance logs
- Uses `$sum` with conditional expressions for statistics
- Filters out departments with no headcount

### Late Leaderboard (competition ranking)
```python
pipeline = [
    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
    {"$match": {"total_late_minutes": {"$gt": 0}}},
    {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    {"$lookup": {...}}  # Get employee details
]
```
- Ranks in Python after MongoDB aggregation
- Competition ranking: ties share rank, next rank skipped
- Filters by rank <= limit (after ranking)

### Department Trend (with gap-filling)
- Date range generated in Python
- Missing dates filled with zero values
- `$densify` stage could be used for large ranges

## 5. Timezone Handling

### IST Conversion Functions
```python
def compute_ist_datetime_from_epoch(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to IST datetime."""
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    return dt_utc.astimezone(IST)

def compute_ist_date_from_epoch(epoch_ms: int) -> str:
    """Get the IST calendar date from epoch milliseconds."""
    return compute_ist_datetime_from_epoch(epoch_ms).strftime("%Y-%m-%d")
```

### R1: Overnight Shifts
```python
def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift is overnight (end <= start)."""
    start_total = parse_time(shift_start)
    end_total = parse_time(shift_end)
    return end_total <= start_total
```
- Shift end on next calendar day for overnight shifts
- Punch-in date calculation uses IST timezone

## 6. Rounding Implementation

### Half-Up Rounding (R4, R8)
```python
from decimal import Decimal, ROUND_HALF_UP

def round_half_up(value: float, decimals: int = 2) -> float:
    """Round using half-up method (not banker's rounding)."""
    d = Decimal(str(value))
    quantize_str = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))
```
- Uses `Decimal` for precise arithmetic
- `ROUND_HALF_UP` matches Python's `decimal.ROUND_HALF_UP` (not banker's rounding)
- 2 decimals for work_hours (R4)
- 4 decimals for rates (R8)

## 7. Business Rule Implementation Summary

| Rule | Implementation |
|------|----------------|
| R1 | IST timezone, overnight shift handling |
| R2 | Grace period (10 min), floor minutes after grace |
| R3 | Overtime >= 30 min threshold, floor minutes |
| R4 | Half-up rounding to 2 decimals |
| R5 | Half day if work_hours < 4.50 |
| R6 | PRESENT/WFH/ON_DUTY count as present |
| R7 | Working days = Mon-Fri, from joined_on to month end |
| R8 | 2 decimals for values, 4 decimals for rates |
| R9 | Headcount = employees with joined_on <= date |
| R10 | Pagination: page starts at 1, max 100, default 20 |

## 8. Trade-offs

### Single File Architecture
- **Pro**: Simple deployment, no module imports needed
- **Con**: Less modular, harder to test in isolation

### MongoDB Aggregation vs Python Computation
- **Pro**: Scales to 100k+ records, leverage MongoDB indexing
- **Con**: More complex queries, harder to debug

### IST Timezone in Application
- **Pro**: Consistent date calculations across all clients
- **Con**: Assumes all users are in IST timezone

### Database Constraints for Uniqueness
- **Pro**: Atomic, no race conditions
- **Con**: Error handling requires try/except
