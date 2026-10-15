"""Employee Attendance & Analytics API for the HROne trainee assignment.

Run from repository root: uvicorn app.main:app --port 8000
Configure MONGO_URI and MONGO_DB in the environment (or a local .env file).
"""
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENT, "ABSENT", "LEAVE")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(value: Optional[int]):
    if value is None:
        return None
    if type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise ValueError("timestamp must be epoch milliseconds as an integer")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def serialize_doc(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("created_at", "punch_in", "punch_out"):
        if key in doc:
            doc[key] = to_ms(doc[key])
    doc.setdefault("punch_in", None)
    doc.setdefault("punch_out", None)
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    for entry in doc["history"]:
        entry["at"] = to_ms(entry.get("at"))
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = to_ms(change.get("from"))
                change["to"] = to_ms(change.get("to"))
    return doc


def round_half_up(value, places=2):
    if value is None:
        return None
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def mongo_round_half_up(expression, places=4):
    """Mongo expression for non-negative values, using half-up rather than $round's half-even."""
    factor = 10 ** places
    return {"$divide": [{"$trunc": [{"$multiply": [{"$add": [expression, 0.5 / factor]}, factor]}]}, factor]}


def valid_date(value: str, field="date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start.isoformat(), end.isoformat(), start, end


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5)


def shift_datetime(day: str, shift: str, overnight=False):
    d = date.fromisoformat(day)
    h, m = map(int, shift.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + (timedelta(days=1) if overnight else timedelta())


def late_minutes(punch_in: datetime, shift_start: str, shift_end: str):
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    sh, sm = map(int, shift_end.split(":"))
    overnight = (sh, sm) <= (h, m)
    shift_day = local.date() - timedelta(days=1) if overnight and (local.hour, local.minute) < (sh, sm) else local.date()
    start = datetime.combine(shift_day, datetime.min.time(), IST).replace(hour=h, minute=m)
    seconds = (local - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str):
    sh, sm = map(int, shift_start.split(":")); eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    end = shift_datetime(attendance_day, shift_end, overnight)
    minutes = int((punch_out.astimezone(IST) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def work_hours(punch_in, punch_out):
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


class StrictModel(BaseModel):
    # The contract says unknown/read-only derived fields are ignored at the API boundary.
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls, v):
        date.fromisoformat(v)
        if len(v) != 10: raise ValueError("joined_on must be YYYY-MM-DD")
        return v
    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None: from_ms(v)
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None: from_ms(v)
        return v


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ts(cls, v):
        if v is not None: from_ms(v)
        return v


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_emp_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_late_date")


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
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    for x in items: x["created_at"] = to_ms(x.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    local = ts.astimezone(IST)
    sh, sm = map(int, emp["shift_start"].split(":")); eh, em = map(int, emp["shift_end"].split(":"))
    overnight = (eh, em) <= (sh, sm)
    attendance_date = local.date()
    if overnight and (local.hour, local.minute) < (eh, em): attendance_date -= timedelta(days=1)
    day = attendance_date.isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return serialize_doc(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: valid_date(date_from, "date_from")
    if date_to: valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size))
    return {"items": [serialize_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    local_date = ts.astimezone(IST).date().isoformat()
    # Include the current IST day and prior day for overnight shifts.
    candidates = list(db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "date": {"$gte": (date.fromisoformat(local_date)-timedelta(days=1)).isoformat(), "$lte": local_date}}).sort("punch_in", DESCENDING).limit(1))
    if not candidates:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None}, "punch_out": {"$ne": None}, "date": {"$gte": (date.fromisoformat(local_date)-timedelta(days=1)).isoformat(), "$lte": local_date}}):
            raise HTTPException(409, "attendance record already punched out")
        raise HTTPException(404, "no punch-in found")
    rec = candidates[0]
    if rec.get("punch_out") is not None: raise HTTPException(409, "attendance record already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = work_hours(rec["punch_in"], ts)
    overtime = overtime_minutes(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    updated = db.attendance_logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "attendance record already punched out")
    return serialize_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "attendance record not found")
    payload = body.model_dump(exclude_unset=True)
    reason = payload.pop("reason"); by = payload.pop("regularized_by")
    for time_field in ("punch_in", "punch_out"):
        if time_field in payload and payload[time_field] is None:
            raise HTTPException(422, f"{time_field} must be epoch milliseconds when supplied")
    old = {k: rec.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    status = payload.get("status", rec.get("status"))
    pin = from_ms(payload["punch_in"]) if "punch_in" in payload else rec.get("punch_in")
    pout = from_ms(payload["punch_out"]) if "punch_out" in payload else rec.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in payload or "punch_out" in payload: raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and pin.astimezone(IST).date().isoformat() != date:
        raise HTTPException(422, "punch_in must fall on attendance date in IST")
    if pout is not None and (pin is None or pout <= pin or pout-pin > timedelta(hours=24)):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = late_minutes(pin, emp["shift_start"], emp["shift_end"]) if status in PRESENT and pin else 0
    hours = work_hours(pin, pout) if status in PRESENT and pin and pout else None
    overtime = overtime_minutes(pout, emp["shift_start"], emp["shift_end"], date) if hours is not None else 0
    half = bool(hours is not None and hours < 4.5)
    new = {"status": status, "punch_in": pin, "punch_out": pout, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for key, value in new.items():
        before = old.get(key)
        if key in ("punch_in", "punch_out"):
            if before is not None and value is not None:
                equal = before == value
            else: equal = before is value
        else: equal = before == value
        if not equal:
            changes[key] = {"from": before, "to": value}
    if not changes: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": by, "reason": reason, "changes": changes}
    # Compare-and-swap prevents concurrent edits from losing each other's audit entries.
    query = {"_id": rec["_id"]}
    for k in old:
        query[k] = rec.get(k)
    update = {"$set": new, "$push": {"history": entry}}
    result = db.attendance_logs.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record changed concurrently; retry")
    return serialize_doc(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end, start_d, end_d = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    wd_start = max(start_d, date.fromisoformat(emp["joined_on"]))
    wd = working_days(wd_start, end_d) if wd_start <= end_d else 0
    pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
    got = list(db.attendance_logs.aggregate(pipe)); v = got[0] if got else {}
    present = round_half_up(v.get("present_days", 0), 2)
    pct = round_half_up(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present, "leave_days": v.get("leave_days", 0), "late_count": v.get("late_count", 0), "total_late_minutes": v.get("total_late_minutes", 0), "total_overtime_minutes": v.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end, start_d, end_d = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    pipe = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "dept_join": "$joined_on"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}, {"$gte": ["$date", "$$dept_join"]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs", None]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
          "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
          "hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": ["$hours_n", 0]}, {"$divide": ["$hours_sum", "$hours_n"]}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    items = list(db.employees.aggregate(pipe))
    # Headcount must be unique employees; grouping after lookup counts one row per log. Recompute headcount from employee group.
    for item in items:
        item["headcount"] = db.employees.count_documents({"department": item["department"], "joined_on": {"$lte": end}})
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None: item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": [x for x in items if x["headcount"] > 0]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end, _, _ = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start_d = valid_date(from_date, "from")
    end_d = valid_date(to_date, "to")
    if end_d < start_d:
        raise HTTPException(422, "to must be on or after from")
    days = (end_d - start_d).days + 1
    if days > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    # The date spine is generated in MongoDB; no Python loop creates daily rows.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}}},
        {"$set": {"offsets": {"$range": [0, days]}}},
        {"$unwind": "$offsets"},
        {"$set": {"day": {"$dateToString": {
            "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$offsets"}},
            "format": "%Y-%m-%d"
        }}}},
        {"$set": {"headcount": {"$size": {"$filter": {
            "input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$day"]}
        }}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$day"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"}, {"$match": {"emp.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"daily": {"$ifNull": [{"$arrayElemAt": ["$daily", 0]}, {"present_count": 0, "late_count": 0}]}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$day"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$day"}}}, 6]}]}, "present_count": "$daily.present_count", "late_count": "$daily.late_count"}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, mongo_round_half_up("$moving_avg_7d", 4)]}}},
        {"$sort": {"date": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        exp = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if not month: raise HTTPException(422, "month is required")
    start, end, _, _ = month_bounds(month)
    if endpoint == "employee_monthly":
        if not emp_code: raise HTTPException(422, "emp_code is required")
        pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_summary":
        q = {"joined_on": {"$lte": end}}
        if department: q["department"] = department
        exp = db.command("explain", {"find": "employees", "filter": q}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "late_leaderboard":
        pipe = [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        pipe = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}, {"$group": {"_id": "$date", "n": {"$sum": 1}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    raise HTTPException(422, "unsupported endpoint")
