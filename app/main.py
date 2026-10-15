"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this file:
  config & indexes -> time helpers (R1) -> business rules (R2-R5) -> models
  -> serializers -> employees & attendance endpoints -> analytics -> admin/explain
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# serverSelectionTimeoutMS: give up after 5 s if MongoDB is unreachable (the default is 30 s).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_ms(dt):
    """Datetime -> epoch milliseconds as an exact integer. None stays None."""
    if dt is None:
        return None
    return (dt - EPOCH) // timedelta(milliseconds=1)


def ensure_indexes():
    """Create the indexes we rely on. Safe to run on every startup (idempotent)."""
    # Unique emp_code: the database itself rejects a duplicate, even when two
    # creates arrive at the same instant.
    db.employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    # At most one attendance record per employee per day; also makes two
    # simultaneous punch-ins safe (only one insert can succeed).
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")
    # GET /attendance sorts by date desc, then emp_code asc; this index returns rows
    # already in that order (and serves date-range filters).
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")
    # GET /attendance?status=... with the same sort.
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    # Punch-out: "this employee's latest record with punch_in <= t".
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punchin")
    # Analytics start from employees (so people with zero logs are counted, R9):
    # "who had joined by the end of the period", with or without a department filter.
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined")
    db.employees.create_index([("joined_on", 1)], name="joined_on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()  # runs once when the server starts
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (R1)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def now_utc() -> datetime:
    """Current time in UTC, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def hhmm(value: str) -> time:
    """'09:30' -> time(9, 30)."""
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    """A shift like 22:00-06:00 ends on the next calendar day (R1)."""
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in. For an overnight shift, a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Real start and end instants of the shift that starts on `day` (IST)."""
    start = datetime.combine(day, hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, hhmm(emp["shift_end"]), tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)  # an overnight shift ends the next morning
    return start, end


# --------------------------------------------------------------------------- #
# Business-rule helpers (R2 - R5, R8)
# --------------------------------------------------------------------------- #
def round_half_up(value, places: int = 2) -> float:
    """R4 / R8: school rounding (2.675 -> 2.68). Python's round() would give 2.67."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then the
    whole minutes since shift start (not since the end of the grace period)."""
    late = punch_in - shift_start
    if late <= timedelta(minutes=10):
        return 0
    return late // timedelta(minutes=1)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only when that is >= 30."""
    minutes = (punch_out - shift_end) // timedelta(minutes=1)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in hours, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / 3600, 2)


def is_half_day(work_hours: float) -> bool:
    """R5: a half day when the rounded work hours are below 4.50."""
    return work_hours < 4.5


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"   # 24-hour time like 09:30


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v):
        date.fromisoformat(v)  # rejects impossible dates like 2026-02-30
        return v

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# An instant in the API: integer epoch milliseconds. StrictInt rejects strings and
# floats; the range rejects seconds-looking values (below 100000000000).
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def _real_day(value: str) -> str:
    date.fromisoformat(value)  # the pattern checks the shape; this rejects 2026-02-30
    return value


# Query-parameter types shared by several endpoints (R10 for pagination).
Day = Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_day)]
DayPath = Annotated[str, Path(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_day)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]  # YYYY-MM


def reject(loc: tuple, msg: str):
    """Return 422 in the same shape as FastAPI's own validation errors."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Only these can be changed; derived fields sent by a client are ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers: MongoDB document -> API response
# --------------------------------------------------------------------------- #
def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # times nested inside history are dates too
            change = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        changes[field] = change
    return {"at": to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def record_out(doc: dict) -> dict:
    """Attendance document -> AttendanceRecord: no _id, instants as epoch ms, and
    missing legacy fields filled with their defaults (DATA_MODEL.md)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Employees & attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")  # ask MongoDB to answer
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)  # the unique index decides who wins
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20,ge=1, le=100),):
    q = {}
    if department:
        q["department"] = department
    skip = (page-1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code",1).skip(skip).limit(page_size))
    for e in items:
        e["created_at"] = to_ms(e.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)  # R1, including overnight shifts
    shift_start, _ = shift_window(day, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # the unique (emp_code, date) index decides races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # The latest record that started at or before this punch-out. Searching by the
    # punch_in instant (not by date) is what makes overnight shifts work.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": ts}}):
            reject(("body", "punched_at"), "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = rec["punch_in"].replace(microsecond=0)
    if ts <= punch_in_at:
        reject(("body", "punched_at"), "punched_at must be after punch_in")
    if ts - punch_in_at > timedelta(hours=24):
        reject(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")

    _, shift_end = shift_window(date.fromisoformat(rec["date"]), emp)
    work_hours = compute_work_hours(punch_in_at, ts)
    # Atomic: only succeeds while punch_out is still empty, so of two simultaneous
    # punch-outs exactly one updates the record and the other gets 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(ts, shift_end),
            "half_day": is_half_day(work_hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """The MongoDB filter for GET /attendance (also reused by the explain endpoint)."""
    if date_from and date_to and date_from > date_to:
        reject(("query", "date_from"), "date_from must be on or before date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from  # YYYY-MM-DD strings sort like dates
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[Day] = None,
    date_to: Optional[Day] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    # Sort, skip and limit run inside MongoDB (on an index): only one page is loaded.
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [record_out(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
TRACKED = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


def to_day(value: str) -> date:
    return date.fromisoformat(value)


def derived_fields(emp: dict, day: date, status: str, punch_in, punch_out) -> dict:
    """R2-R5 for a record's final state (DATA_MODEL.md, 'For a record your own code writes')."""
    if status not in PRESENCE:  # ABSENT / LEAVE
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    shift_start, shift_end = shift_window(day, emp)
    fields = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, shift_start),
              "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields.update(work_hours=hours, overtime_minutes=compute_overtime(punch_out, shift_end),
                      half_day=is_half_day(hours))
    return fields


def _whole_seconds(dt):
    return dt.replace(microsecond=0) if dt is not None else None


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DayPath, body: RegularizeIn):
    # Note: inside this function `date` is the path value (a string), not the date class.
    for field in ("status", "punch_in", "punch_out"):
        if field in body.model_fields_set and getattr(body, field) is None:
            reject(("body", field), f"{field} cannot be null")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # Current values; missing legacy fields count as their defaults.
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    # Final state after the correction. Omitted fields keep their current value.
    status = body.status or old["status"]
    if status not in PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            reject(("body",), "ABSENT and LEAVE clear both punch times; do not send punch_in or punch_out")
        punch_in = punch_out = None
    else:
        punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if punch_in is None:
            reject(("body", "punch_in"), f"status {status} requires a punch_in")
        if attendance_date(punch_in, emp).isoformat() != date:
            reject(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if punch_out is not None:
            if punch_out <= punch_in:
                reject(("body", "punch_out"), "punch_out must be after punch_in")
            if punch_out - punch_in > timedelta(hours=24):
                reject(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
           **derived_fields(emp, to_day(date), status, _whole_seconds(punch_in), _whole_seconds(punch_out))}

    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED if old[f] != new[f]}
    if not changes:
        reject(("body",), "the request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic locking: update only if the record still looks exactly as we read it.
    # Every successful correction appends one history entry, so the history length
    # works as a version number. A concurrent edit makes this match nothing -> 409,
    # instead of one request silently overwriting the other's history.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; reload and retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31)."""
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekdays_between(start: date, end: date) -> int:
    """Number of Mon-Fri days from start to end, both included (R7)."""
    if start > end:
        return 0
    full_weeks, extra = divmod((end - start).days + 1, 7)
    count = full_weeks * 5
    for i in range(extra):  # at most 6 leftover days
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count


# Aggregation building blocks, reused by several pipelines.
# Attendance dates are 'YYYY-MM-DD' strings; $isoDayOfWeek gives 1 = Monday ... 7 = Sunday.
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}  # R6
# R5: a half day counts as 0.5 of a present day; a missing half_day means false.
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}  # present-status with a value


def department_summary_pipeline(first: date, last: date, department: Optional[str] = None) -> list:
    """Runs on `employees`, so employees with zero logs still appear (R9)."""
    who = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined by the end of the month
    if department:
        who["department"] = department
    return [
        # 1. Everyone who counts for headcount this month.
        {"$match": who},
        # 2. One document per department: its headcount (no-log employees included)
        #    and the list of its employees' codes.
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        # 3. JOIN once per department (not once per employee): `codes` is an array, so this
        #    matches logs of ANY of those employees, via the (emp_code, date) index.
        #    The month's logs are totalled inside the join, so `month` holds 0 or 1 document.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
                    # Sum and count kept separately so the department average is over
                    # records, not an average of per-employee averages. Decimal = exact sum.
                    "wh_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_n": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": LATE},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "month",
        }},
        # 4. A department whose employees have no logs this month gets an empty array: keep it.
        {"$unwind": {"path": "$month", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "headcount": 1,
            "present_days": {"$ifNull": ["$month.present_days", 0]},
            "wh_sum": {"$ifNull": ["$month.wh_sum", 0]},
            "wh_n": {"$ifNull": ["$month.wh_n", 0]},
            "late_count": {"$ifNull": ["$month.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$month.total_late_minutes", 0]},
            "leave_count": {"$ifNull": ["$month.leave_count", 0]},
            "on_duty_count": {"$ifNull": ["$month.on_duty_count", 0]},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        # 1. Late records in the month (date range served by the date index).
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        # 2. One row per employee: total late minutes and how many late records.
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # 3. Name and department come from employees; logs of unknown employees drop out here.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        # Filter BEFORE ranking, so the ranking happens inside the department only.
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # 4. Standard competition ranking: ties share a rank and the next rank is skipped
        #    (1, 2, 2, 4). $rank does exactly that; the array position would not.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # 5. limit applies AFTER ranking: keep everyone whose rank <= limit (ties included).
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def half_up_4(expr) -> dict:
    """R8 for rates: 4 decimals, half-up, done in MongoDB with Decimal128.
    ($round rounds a 5 to the nearest even digit, which is not half-up.)"""
    return {"$divide": [{"$trunc": {"$add": [{"$multiply": [expr, 10000]}, Decimal128("0.5")]}}, 10000]}


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    """One row per calendar day from start to end, built entirely in MongoDB."""
    s, e = start.isoformat(), end.isoformat()
    joined = {"$lte": ["$joined_on", e]}
    return [
        # 1. The department's employees (served by the department + joined_on index).
        {"$match": {"department": department}},
        # 2. Gather the department into ONE document: the employee codes, and a headcount
        #    event per employee: +1 on the day they joined (clamped to the first day) if they
        #    joined by the last day; otherwise a harmless 0 on the first day.
        {"$group": {
            "_id": None,
            "codes": {"$push": "$emp_code"},
            "joins": {"$push": {
                "day": {"$cond": [joined, {"$max": ["$joined_on", s]}, s]},
                "joins": {"$cond": [joined, 1, 0]},
            }},
        }},
        # 3. JOIN once: all of those employees' records in the range (`codes` is an array,
        #    served by the (emp_code, date) index), already totalled per day, so this array
        #    has at most one small entry per day.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": s, "$lte": e}}},
                {"$group": {
                    "_id": "$date",
                    "present": {"$sum": PRESENT_VALUE},  # any weekday or weekend; half day = 0.5
                    "late": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
                }},
                {"$project": {"_id": 0, "day": "$_id", "present": 1, "late": 1}},
            ],
            "as": "logs",
        }},
        # 4. One list of dated "events": a zero marker on the first day (guarantees at least
        #    one row for $densify), the headcount events, and the per-day attendance totals.
        {"$project": {"events": {"$concatArrays": [[{"day": s, "joins": 0}], "$joins", "$logs"]}}},
        {"$unwind": "$events"},
        # 5. One document per day that has any event.
        {"$group": {
            "_id": "$events.day",
            "joins": {"$sum": "$events.joins"},
            "present_count": {"$sum": "$events.present"},
            "late_count": {"$sum": "$events.late"},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # 6. GAP FILLING: add a document for every missing day in [start, end].
        #    The upper bound is exclusive, hence end + 1 day.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [
            datetime.combine(start, time(), tzinfo=UTC),
            datetime.combine(end + timedelta(days=1), time(), tzinfo=UTC),
        ]}}},
        # 7. R9 headcount for each day = running total of joins up to that day.
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},  # filled-in days have no events
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # 8. Rate only on working days with headcount > 0; a working day with no records is 0.0.
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_4({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # 9. Mean of the non-null rates in this row and up to 6 rows before it. The window
        #    only sees rows inside the range, so the first rows use a shorter window.
        #    $avg skips nulls and returns null when there is nothing to average.
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, half_up_4("$moving_avg_7d")]},
        }},
        {"$sort": {"date": 1}},
    ]


def as_decimal(value) -> Decimal:
    """MongoDB Decimal128 (or a plain number) -> Python Decimal."""
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        # 1. Only this employee's records in this month (served by the emp_code + date index).
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        # 2. Add everything up in one pass.
        {"$group": {
            "_id": None,
            # R7: present_days counts Mon-Fri records only...
            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # ...but late and overtime totals include weekend records.
            "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": OVERTIME},
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    # R7: working days are counted from joined_on for mid-month joiners.
    working_days = weekdays_between(max(first, to_day(emp["joined_on"])), last)

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    totals = rows[0] if rows else {}
    present_days = round_half_up(totals.get("present_days", 0), 2)
    pct = None
    if working_days:  # R8: computed with Decimal so half-up rounding is exact
        pct = round_half_up(Decimal(str(present_days)) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        avg = None
        if row["wh_n"]:
            avg = round_half_up(as_decimal(row["wh_sum"]) / row["wh_n"], 2)
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
def _number(value):
    """Decimal128 from MongoDB -> float for JSON; other values unchanged."""
    return float(value.to_decimal()) if isinstance(value, Decimal128) else value


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = to_day(start), to_day(end)
    if last < first:
        reject(("query", "to"), "to must be on or after from")
    if (last - first).days + 1 > 92:
        reject(("query", "to"), "the range can be at most 92 days")
    return first, last


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_day)],
    end: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_day)],
):
    first, last = trend_range(start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(department_trend_pipeline(department, first, last))
    items = [{k: _number(v) for k, v in row.items()} for row in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _opt_real_day(value):
    return None if value is None else _real_day(value)


DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _require(**params):
    """422 for any required parameter that is missing."""
    for name, value in params.items():
        if value is None:
            reject(("query", name), f"{name} is required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[Day] = None,
    date_to: Optional[Day] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN), AfterValidator(_opt_real_day)] = None,
    end: Annotated[Optional[str], Query(alias="to", pattern=DATE_PATTERN), AfterValidator(_opt_real_day)] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Explain (executionStats) the main query of an endpoint, built by the SAME function
    the endpoint uses, so the plan shown is the plan that endpoint really runs."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        first, last = month_bounds(month)
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        _require(month=month)
        collection = "employees"
        first, last = month_bounds(month)
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(first, last, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        collection = "attendance_logs"
        first, last = month_bounds(month)
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(first, last, limit, department),
                   "cursor": {}}
    else:  # department_trend
        _require(department=department, **{"from": start, "to": end})
        collection = "employees"
        first, last = trend_range(start, end)
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, first, last), "cursor": {}}

    explain = db.command("explain", command, verbosity="executionStats")
    # json_util turns BSON-only types (Timestamp, ObjectId, dates...) into plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explain))}
