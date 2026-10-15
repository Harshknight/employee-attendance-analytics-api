"""Employee Attendance & Analytics API (FastAPI + MongoDB).

Run from the repository root:
    uvicorn app.main:app --port 8000
"""
import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENCE + ("ABSENT", "LEAVE")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def bad(detail: str, code: int = 422):
    raise HTTPException(status_code=code, detail=detail)


def parse_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        bad(f"{label} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        bad(f"{label} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        bad("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_shift(value: str, label: str = "shift time") -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        bad(f"{label} must be HH:MM")
    h, m = map(int, value.split(":"))
    return h, m


def epoch_ms_to_datetime(value: Any, field: str = "punched_at") -> datetime:
    # bool is a subclass of int, so reject it explicitly.
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        bad(f"{field} must be an integer epoch-millisecond value between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        bad(f"{field} is out of range")


def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def as_utc_aware(value: datetime) -> datetime:
    # MongoDB stores UTC BSON dates; PyMongo returns naive UTC datetimes by default.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc_aware(value).timestamp() * 1000)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            total += 1
        cursor += timedelta(days=1)
    return total


def shift_start_instant(attendance_date: str, shift_start: str) -> datetime:
    h, m = parse_shift(shift_start, "shift_start")
    d = parse_date(attendance_date)
    return datetime.combine(d, time(h, m), IST).astimezone(UTC).replace(tzinfo=None)


def shift_end_instant(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    start_h, start_m = parse_shift(shift_start, "shift_start")
    end_h, end_m = parse_shift(shift_end, "shift_end")
    d = parse_date(attendance_date)
    if (end_h, end_m) <= (start_h, start_m):
        d += timedelta(days=1)
    return datetime.combine(d, time(end_h, end_m), IST).astimezone(UTC).replace(tzinfo=None)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = as_utc_aware(punch_in).astimezone(IST)
    start_h, start_m = parse_shift(shift_start, "shift_start")
    end_h, end_m = parse_shift(shift_end, "shift_end")
    local_date = local.date()
    if (end_h, end_m) <= (start_h, start_m) and (local.hour, local.minute) < (end_h, end_m):
        return local_date - timedelta(days=1)
    return local_date


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: Optional[str] = None) -> int:
    punch_in = punch_in.replace(microsecond=0)
    local = as_utc_aware(punch_in).astimezone(IST)
    h, m = parse_shift(shift_start, "shift_start")
    shift_day = parse_date(attendance_day) if attendance_day else local.date()
    start = datetime.combine(shift_day, time(h, m), IST)
    seconds = (local - start).total_seconds()
    if seconds <= 600:
        return 0
    return int(seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal("3600"), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    punch_out = punch_out.replace(microsecond=0)
    end = shift_end_instant(attendance_day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def api_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_epoch_ms(result["created_at"])
    return result


def api_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["punch_in"] = to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = to_epoch_ms(result.get("punch_out"))
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])
        for change in (entry.get("changes") or {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_epoch_ms(change[side])
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return result


def json_safe(value: Any) -> Any:
    # Convert ObjectIds, datetimes and BSON values in explain output to JSON-compatible values.
    return json.loads(json_util.dumps(value))


def validate_page(page: int, page_size: int):
    if page < 1:
        bad("page must be at least 1")
    if page_size < 1 or page_size > 100:
        bad("page_size must be between 1 and 100")


# ---------------------------------- models ----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_format(cls, value):
        parse_shift(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def joined_date_format(cls, value):
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            bad("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_punch_time(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value

    @field_validator("status")
    @classmethod
    def valid_presence_status(cls, value):
        if value not in PRESENCE:
            bad("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_punch_time(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            bad("Invalid status")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_epoch(cls, value, info):
        if value is not None:
            epoch_ms_to_datetime(value, info.field_name)
        return value


# ----------------------------- MongoDB indexes ------------------------------
def create_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    attendance_logs.create_index([("punch_in", DESCENDING)], name="attendance_punch_in")


@app.on_event("startup")
def startup():
    create_indexes()


# ---------------------------------- helpers ---------------------------------
def month_filter(month: str) -> tuple[str, str]:
    first, last = parse_month(month)
    return first.isoformat(), last.isoformat()


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from, "date_from")
            q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to, "date_to")
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            bad("date_from must be on or before date_to")
    if status:
        if status not in STATUSES:
            bad("Invalid status")
        q["status"] = status
    return q


def present_fraction_expr():
    return {"$cond": [
        {"$in": ["$status", list(PRESENCE)]},
        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
        0.0,
    ]}


def is_weekday_expr(date_field: str):
    # $dayOfWeek: Sunday=1, Saturday=7.
    return {"$and": [{"$gte": [{"$dayOfWeek": date_field}, 2]}, {"$lte": [{"$dayOfWeek": date_field}, 6]}]}


def explain_aggregate(collection, pipeline):
    command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    return db.command({"explain": command, "verbosity": "executionStats"})


def working_days_for_employee(joined_on: str, month_start: date, month_end: date) -> int:
    joined = parse_date(joined_on, "joined_on")
    start = max(joined, month_start)
    return working_days(start, month_end) if start <= month_end else 0


# ---------------------------------- system ----------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# -------------------------------- employees ---------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    validate_page(page, page_size)
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_employee(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance ---------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = ts.replace(microsecond=0)
    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return api_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = ts.replace(microsecond=0)
    # Pick the most recent punch-in at or before this instant, including yesterday's overnight shift.
    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    elapsed = (ts - record["punch_in"].replace(microsecond=0)).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        bad("punched_at must be after punch_in and no more than 24 hours later")
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50
    updated = attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)
    q = attendance_query(emp_code, date_from, date_to, status)
    total = attendance_logs.count_documents(q)
    cursor = attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_attendance(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    old = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    patch = body.model_dump(exclude_unset=True)
    reason, by = patch.pop("reason"), patch.pop("regularized_by")
    if not patch:
        bad("At least one attendance field must be changed")
    if patch.get("status") in ("ABSENT", "LEAVE") and ("punch_in" in patch or "punch_out" in patch):
        bad("ABSENT and LEAVE cannot be supplied with punch times")
    final_status = patch.get("status", old["status"])
    if final_status in ("ABSENT", "LEAVE"):
        final_in = final_out = None
    else:
        final_in = epoch_ms_to_datetime(patch["punch_in"], "punch_in") if "punch_in" in patch and patch["punch_in"] is not None else old.get("punch_in")
        final_out = epoch_ms_to_datetime(patch["punch_out"], "punch_out") if "punch_out" in patch and patch["punch_out"] is not None else old.get("punch_out")
        if final_in is None:
            bad("Presence status requires punch_in")
        if final_in.astimezone(IST).date().isoformat() != date if final_in.tzinfo else final_in.replace(tzinfo=UTC).astimezone(IST).date().isoformat() != date:
            bad("punch_in must fall on the record's attendance date")
        if final_out is not None:
            seconds = (final_out - final_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                bad("punch_out must be after punch_in and within 24 hours")
    # Convert epoch values to BSON UTC datetimes in the update.
    final_values = {"status": final_status, "punch_in": final_in, "punch_out": final_out}
    final_values["late_minutes"] = compute_late_minutes(final_in, emp["shift_start"], date) if final_in else 0
    if final_in and final_out:
        final_values["work_hours"] = compute_work_hours(final_in, final_out)
        final_values["overtime_minutes"] = compute_overtime(final_out, emp["shift_start"], emp["shift_end"], date)
        final_values["half_day"] = final_values["work_hours"] < 4.50
    else:
        final_values["work_hours"] = None
        final_values["overtime_minutes"] = 0
        final_values["half_day"] = False
    # Only explicitly supplied status/times can change; derived values are recalculated.
    changed = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before = old.get(field, False if field == "half_day" else 0 if field in ("late_minutes", "overtime_minutes") else None)
        after = final_values[field]
        if before != after:
            changed[field] = {"from": before, "to": after}
    if not changed:
        bad("The request does not change the record")
    now = now_utc_naive()
    history_entry = {"at": now, "by": by, "reason": reason, "changes": changed}
    # Compare-and-set avoids losing concurrent corrections/history. A loser receives 409.
    result = attendance_logs.update_one(
        {"emp_code": emp_code, "date": date, "_id": old["_id"], "history": old.get("history", [])},
        {"$set": final_values, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry the correction")
    updated = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return api_attendance(updated)


# ------------------------------- analytics ----------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_start, month_end = parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    start = max(month_start, parse_date(emp["joined_on"], "joined_on"))
    workdays = working_days(start, month_end) if start <= month_end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$addFields": {"_weekday": is_weekday_expr({"$dateFromString": {"dateString": "$date"}}), "_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, "$_weekday"]}, {"$cond": ["$_half", 0.5, 1.0]}, 0.0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}},
    ]
    row = next(attendance_logs.aggregate(pipeline), {})
    present = round_half_up(row.get("present_days", 0), 2)
    pct = round_half_up(Decimal(str(present)) / Decimal(workdays) * 100, 2) if workdays else None
    return {"emp_code": emp_code, "month": month, "working_days": workdays, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]):
    match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", month_start]}, {"$lte": ["$date", month_end]}]}}},
            {"$addFields": {"_weekday": is_weekday_expr({"$dateFromString": {"dateString": "$date"}}), "_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
        ], "as": "logs"}},
        {"$addFields": {
            "_present": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, "$$l._weekday"]}, {"$cond": ["$$l._half", 0.5, 1.0]}, 0.0]}}}},
            "_late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": ["$$l._late", 0]}}}},
            "_late_total": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": "$$l._late"}}},
            "_leave_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}},
            "_work_values": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$ne": ["$$l.work_hours", None]}]}}}
        }},
        {"$addFields": {"_work_sum": {"$sum": {"$map": {"input": "$_work_values", "as": "l", "in": "$$l.work_hours"}}}, "_work_count": {"$size": "$_work_values"}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$_present"},
            "late_count": {"$sum": "$_late_count"}, "total_late_minutes": {"$sum": "$_late_total"},
            "leave_count": {"$sum": "$_leave_count"}, "on_duty_count": {"$sum": "$_duty_count"},
            "work_sum": {"$sum": "$_work_sum"}, "work_count": {"$sum": "$_work_count"}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$round": [{"$divide": ["$work_sum", "$work_count"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_filter(month)
    pipeline = department_summary_pipeline(start, end, department)
    items = list(employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str]):
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_filter(month)
    pipeline = leaderboard_pipeline(start, end, limit, department)
    items = list(attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, from_date: date, to_date: date):
    day_count = (to_date - from_date).days + 1
    return [
        {"$match": {"department": department}},
        {"$addFields": {
            "_joined": {"$dateFromString": {"dateString": "$joined_on"}},
            "_start": {"$dateFromString": {"dateString": {"$literal": from_date.isoformat()}}},
        }},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
        {"$project": {
            "joined": "$_joined",
            "days": {"$map": {
                "input": {"$range": [0, day_count]}, "as": "n",
                "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": "$_start", "unit": "day", "amount": "$$n"}}}},
            }},
            "logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [
                {"$gte": ["$$l.date", from_date.isoformat()]}, {"$lte": ["$$l.date", to_date.isoformat()]}
            ]}}},
        }},
        {"$unwind": "$days"},
        {"$addFields": {
            "_day_date": {"$dateFromString": {"dateString": "$days"}},
            "_day_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.date", "$days"]}}},
        }},
        {"$group": {
            "_id": "$days",
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined", "$_day_date"]}, 1, 0]}},
            "present_count": {"$sum": {"$sum": {"$map": {
                "input": "$_day_logs", "as": "l",
                "in": {"$cond": [
                    {"$in": ["$$l.status", list(PRESENCE)]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1.0]},
                    0.0,
                ]},
            }}}},
            "late_count": {"$sum": {"$size": {"$filter": {
                "input": "$_day_logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
            }}}},
        }},
        {"$addFields": {"is_working_day": is_weekday_expr({"$dateFromString": {"dateString": "$_id"}})}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1,
            "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$sort": {"date": 1}},
    ]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        query = {"find": q, "sort": [("date", DESCENDING), ("emp_code", ASCENDING)], "skip": (page - 1) * page_size, "limit": page_size}
        raw = db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            bad("emp_code and month are required for employee_monthly")
        start, end = month_filter(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}]
        raw = explain_aggregate(attendance_logs, pipeline)
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            bad("month is required for department_summary")
        start, end = month_filter(month)
        pipeline = department_summary_pipeline(start, end, department)
        raw = explain_aggregate(employees, pipeline)
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            bad("month is required for late_leaderboard")
        start, end = month_filter(month)
        pipeline = leaderboard_pipeline(start, end, limit, department)
        raw = explain_aggregate(attendance_logs, pipeline)
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            bad("department, from and to are required for department_trend")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start or (end - start).days + 1 > 92:
            bad("Invalid department trend date range")
        pipeline = department_trend_pipeline(department, start, end)
        raw = explain_aggregate(employees, pipeline)
        collection = "employees"
    else:
        bad("endpoint must be one of attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend")
    return {"endpoint": endpoint, "collection": collection, "explain": json_safe(raw)}


@app.get("/analytics/departments/{department}/trend")
def department_trend_route(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    start, end = parse_date(from_, "from"), parse_date(to, "to")
    if end < start:
        bad("to must be on or after from")
    if (end - start).days + 1 > 92:
        bad("date range cannot exceed 92 days")
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    items = list(employees.aggregate(department_trend_pipeline(department, start, end)))
    for item in items:
        item["present_count"] = round_half_up(item["present_count"], 2)
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}
