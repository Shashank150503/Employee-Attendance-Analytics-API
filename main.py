"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. configuration and database handle
  2. time / rounding helpers (business rules R1 - R5, R8)
  3. request models and serialisation
  4. query / pipeline builders (shared by the real endpoints AND the explain endpoint)
  5. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from math import floor
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes instead of naive ones, so
# arithmetic with IST datetimes can never silently mix the two.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=10_000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENCE = ["ABSENT", "LEAVE"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = 4.5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]  # strict: no floats, no strings, no bools

MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def _hint(name: str) -> dict:
    """Index hint kwargs. Hints make the plan deterministic (and keep explain honest)."""
    return {"hint": name}


# Index names (created at startup, see ensure_indexes)
ATT_EMP_DATE = "att_emp_date"  # unique (emp_code, date)
ATT_DATE_EMP = "att_date_emp"
ATT_STATUS_DATE_EMP = "att_status_date_emp"
ATT_EMP_PUNCH_IN = "att_emp_punch_in"
EMP_CODE_UQ = "emp_code_uq"
EMP_DEPT_CODE = "emp_dept_code"
EMP_DEPT_JOINED = "emp_dept_joined"
EMP_JOINED = "emp_joined"


def ensure_indexes() -> None:
    """Idempotent: create_index with an identical spec is a no-op if it already exists."""
    specs = [
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], ATT_EMP_DATE, True),
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], ATT_DATE_EMP, False),
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
         ATT_STATUS_DATE_EMP, False),
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], ATT_EMP_PUNCH_IN, False),
        ("employees", [("emp_code", ASCENDING)], EMP_CODE_UQ, True),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], EMP_DEPT_CODE, False),
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], EMP_DEPT_JOINED, False),
        ("employees", [("joined_on", ASCENDING)], EMP_JOINED, False),
    ]
    for coll, keys, name, unique in specs:
        try:
            db[coll].create_index(keys, name=name, unique=unique)
        except PyMongoError as exc:  # never keep the service from booting; /health reports DB trouble
            log.error("could not create index %s on %s: %s", name, coll, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time and rounding helpers
# --------------------------------------------------------------------------- #
def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def truncate_seconds(dt: datetime) -> datetime:  # R1: whole seconds before any calculation
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def instant_or_now(ms: Optional[int]) -> datetime:
    return truncate_seconds(from_ms(ms) if ms is not None else now_utc())


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the
    shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def _at(date_str: str, t: str) -> datetime:
    return datetime.combine(Date.fromisoformat(date_str), hhmm(t), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((aware(punch_in) - _at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only if at least 30. Overnight shifts end the next day."""
    end = _at(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((aware(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value, places: int) -> Decimal:
    """Exact half-up rounding of a rational number (never touches binary floats)."""
    scaled = Fraction(value) * (10 ** places)
    return Decimal(floor(scaled + Fraction(1, 2))).scaleb(-places)


def round_float_half_up(x: float, places: int) -> float:
    """Half-up rounding for a double that came out of MongoDB. The double is first cleaned to 12 places
    so representation noise (0.31244999999999) cannot flip an exact tie (0.31245)."""
    d = Decimal(repr(float(x))).quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float(round_half_up(Fraction(seconds, 3600), 2))


def derive(shift_start: str, shift_end: str, date_str: str,
           punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All stored derived fields, from the final punch times (R2 - R5)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is not None:
        out["late_minutes"] = compute_late_minutes(punch_in, date_str, shift_start)
        if punch_out is not None:
            wh = compute_work_hours(punch_in, punch_out)
            out["work_hours"] = wh
            out["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
            out["half_day"] = wh < HALF_DAY_BELOW
    return out


# --------------------------------------------------------------------------- #
# Validation helpers (FastAPI-style 422 bodies for things Pydantic cannot see)
# --------------------------------------------------------------------------- #
def invalid(name: str, msg: str, where: str = "query"):
    raise RequestValidationError(
        [{"type": "value_error", "loc": (where, name), "msg": msg, "input": None}]
    )


def parse_date(value: str, name: str, where: str = "query") -> Date:
    if not DATE_RE.match(value):
        invalid(name, "must be a date in YYYY-MM-DD format", where)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        invalid(name, "not a valid calendar date", where)


def month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        invalid("month", "year out of range")
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(start: Date, end: Date, joined_on: str) -> int:
    """R7: Mon-Fri between start..end, counted from joined_on for mid-month joiners."""
    d = max(start, Date.fromisoformat(joined_on))
    n = 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Models and serialisation
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_null(cls, data: Any):
        # The contract has no nullable request fields: "omitted" means unchanged, explicit null is invalid.
        if isinstance(data, dict):
            for k in ("status", "punch_in", "punch_out"):
                if k in data and data[k] is None:
                    raise ValueError(f"{k} must not be null; omit it to leave it unchanged")
        return data


def _history_value(field: str, v: Any) -> Any:
    return to_ms(v) if field in ("punch_in", "punch_out") and isinstance(v, datetime) else v


def serialize_log(doc: dict) -> dict:
    """Mongo document -> API record. Tolerates legacy documents (missing history / half_day / ...)."""
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _history_value(k, c.get("from")), "to": _history_value(k, c.get("to"))}
                for k, c in (h.get("changes") or {}).items()
            },
        })
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
        "history": history,
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Query and pipeline builders
# --------------------------------------------------------------------------- #
def plan_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    if emp_code is not None:
        # emp_code is fixed, so ordering by date alone gives the same order; (emp_code, date) index serves it
        sort, hint = {"date": -1}, ATT_EMP_DATE
    else:
        sort = {"date": -1, "emp_code": 1}
        hint = ATT_STATUS_DATE_EMP if status is not None else ATT_DATE_EMP
    return {"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}


# Small aggregation-expression builders. `p` is the prefix that precedes a field name:
#   "$"      -> field of the current document          ("$half_day")
#   "$$l."   -> field of a $filter/$map variable `l`    ("$$l.half_day")
#   "$l."    -> field of a sub-document called `l`      ("$l.half_day")
def _is_weekday(date_expr: str) -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}, 5]}


def _halves(p: str) -> dict:
    """Presence value in half-day units: 1 for a half day, 2 for a full day (keeps sums exact integers)."""
    return {"$cond": [{"$eq": [{"$ifNull": [p + "half_day", False]}, True]}, 1, 2]}


def _is_late(p: str) -> dict:
    return {"$gt": [{"$ifNull": [p + "late_minutes", 0]}, 0]}


def plan_monthly(emp_code: str, month: str) -> tuple[list, str]:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            # R5-R7: presence statuses on Mon-Fri only; a half day counts 0.5 (= 1 unit of 2)
            "present_halves": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE]}, _is_weekday("$date")]}, _halves("$"), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_is_late("$"), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return pipeline, ATT_EMP_DATE


def plan_department_summary(month: str, department: Optional[str]) -> tuple[list, str]:
    start, end = month_bounds(month)
    s, e = start.isoformat(), end.isoformat()
    emp_filter: dict = {"joined_on": {"$lte": e}}  # R9: joined on or before the month end
    if department is not None:
        emp_filter["department"] = department
    hint = EMP_DEPT_JOINED if department is not None else EMP_JOINED

    def filt(cond: dict) -> dict:
        return {"$filter": {"input": "$logs", "as": "l", "cond": cond}}

    present_weekday = {"$and": [{"$in": ["$$l.status", PRESENCE]}, _is_weekday("$$l.date")]}
    present_with_hours = {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$isNumber": "$$l.work_hours"}]}

    pipeline = [
        {"$match": emp_filter},  # starts from employees, so people with zero logs still count (left join)
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": s, "$lte": e}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs"}},
        # per-employee totals, then one $group per department
        {"$addFields": {
            "ph": {"$sum": {"$map": {"input": filt(present_weekday), "as": "l", "in": _halves("$$l.")}}},
            "wn": {"$size": filt(present_with_hours)},
            # work_hours in whole hundredths: exact integer sums, rounded half-up later in Python
            "wc": {"$sum": {"$map": {"input": filt(present_with_hours), "as": "l",
                                     "in": {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}}}},
            "lc": {"$size": filt(_is_late("$$l."))},
            "lt": {"$sum": "$logs.late_minutes"},
            "lv": {"$size": filt({"$eq": ["$$l.status", "LEAVE"]})},
            "od": {"$size": filt({"$eq": ["$$l.status", "ON_DUTY"]})},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": {"$sum": "$ph"},
            "work_n": {"$sum": "$wn"},
            "work_cents": {"$sum": "$wc"},
            "late_count": {"$sum": "$lc"},
            "total_late_minutes": {"$sum": "$lt"},
            "leave_count": {"$sum": "$lv"},
            "on_duty_count": {"$sum": "$od"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, hint


def plan_late_leaderboard(month: str, limit: int, department: Optional[str]) -> tuple[list, str]:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline, ATT_DATE_EMP


def plan_department_trend(department: str, d_from: Date, d_to: Date) -> tuple[list, str]:
    f, t = d_from.isoformat(), d_to.isoformat()
    n_days = (d_to - d_from).days + 1
    day_start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    present_units = {"$cond": [{"$in": ["$l.status", PRESENCE]}, _halves("$l."), 0]}
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": t}}},
        # one document holding (a) headcount buckets by join date and (b) per-day log totals
        {"$facet": {
            "hc": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            "logs": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": f, "$lte": t}}},
                        {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                    ],
                    "as": "l"}},
                {"$unwind": "$l"},
                {"$group": {"_id": "$l.date", "ph": {"$sum": present_units},
                            "lc": {"$sum": {"$cond": [_is_late("$l."), 1, 0]}}}},
            ],
        }},
        # gap filling inside the database: exactly one row per calendar day, weekends and empty days included
        {"$project": {"_id": 0, "hc": 1, "logs": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": {
            "$dateAdd": {"startDate": day_start, "unit": "day", "amount": "$offset"}}}}}},
        {"$addFields": {
            # R9: employees whose joined_on <= this day (ISO date strings compare correctly as text)
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$hc", "as": "h", "cond": {"$lte": ["$$h._id", "$date"]}}},
                "as": "h", "in": "$$h.n"}}},
            "day": {"$first": {"$filter": {"input": "$logs", "as": "g", "cond": {"$eq": ["$$g._id", "$date"]}}}},
        }},
        {"$addFields": {
            "present_halves": {"$ifNull": ["$day.ph", 0]},
            "late_count": {"$ifNull": ["$day.lc", 0]},
            "is_working_day": _is_weekday("$date"),
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_halves", {"$multiply": ["$headcount", 2]}]},
            None]}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_halves": 1,
                      "late_count": 1, "attendance_rate": 1}},
        # mean of the non-null rates in the current row + up to 6 preceding rows (never before the range)
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]
    return pipeline, EMP_DEPT_JOINED


# --------------------------------------------------------------------------- #
# 5. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- employees ---------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created = now_utc()
    doc["created_at"] = created.replace(microsecond=created.microsecond // 1000 * 1000)  # Mongo keeps ms
    try:
        db.employees.insert_one(doc)  # the unique index is what really guarantees uniqueness
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance --------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_or_now(body.punched_at)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_or_now(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)], **_hint(ATT_EMP_PUNCH_IN),
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in:
        invalid("punched_at", "must be after the punch-in", "body")
    if ts - p_in > MAX_SHIFT:
        invalid("punched_at", "more than 24 hours after the punch-in", "body")
    d = derive(emp["shift_start"], emp["shift_end"], rec["date"], p_in, ts)
    # compare-and-set on punch_out: of two simultaneous punch-outs only one matches the filter
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **d}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_log(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    d_from = parse_date(date_from, "date_from") if date_from is not None else None
    d_to = parse_date(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        invalid("date_from", "date_from must not be after date_to")
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    q = plan["filter"]
    if q:
        total = db.attendance_logs.count_documents(q, **_hint(plan["hint"]))
    else:
        total = db.attendance_logs.estimated_document_count()
    cursor = (db.attendance_logs.find(q, {"_id": 0}).sort(list(plan["sort"].items()))
              .skip(plan["skip"]).limit(plan["limit"]).hint(plan["hint"]))
    return {"items": [serialize_log(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _plan_regularization(emp: dict, cur: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Pure function: (new field values, changes) for one correction. Raises 422 on rule violations."""
    old_status = cur["status"]
    new_status = body.status or old_status
    old_in, old_out = aware(cur.get("punch_in")), aware(cur.get("punch_out"))
    req_in = truncate_seconds(from_ms(body.punch_in)) if body.punch_in is not None else None
    req_out = truncate_seconds(from_ms(body.punch_out)) if body.punch_out is not None else None

    if new_status in NON_PRESENCE:
        if req_in is not None or req_out is not None:
            invalid("punch_in", f"punch times cannot be supplied with status {new_status}", "body")
        new_in = new_out = None  # ABSENT / LEAVE clear both punch times
    else:
        new_in = req_in or old_in
        if new_in is None:
            invalid("punch_in", f"status {new_status} requires a punch_in", "body")
        if req_in is not None and attendance_date(req_in, emp["shift_start"], emp["shift_end"]) != cur["date"]:
            invalid("punch_in", "punch_in must stay on the record's attendance date", "body")
        new_out = req_out or old_out
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            invalid("punch_out", "punch_out must be after punch_in and within 24 hours of it", "body")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive(emp["shift_start"], emp["shift_end"], cur["date"], new_in, new_out)}
    old = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
           "work_hours": cur.get("work_hours"), "late_minutes": cur.get("late_minutes") or 0,
           "overtime_minutes": cur.get("overtime_minutes") or 0, "half_day": bool(cur.get("half_day", False))}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        invalid("body", "the request changes nothing", "body")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    for _ in range(5):  # optimistic concurrency: re-read and retry if somebody else wrote in between
        cur = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if cur is None:
            raise HTTPException(404, "no attendance record for this date")
        new, changes = _plan_regularization(emp, cur, body)
        entry = {"at": truncate_seconds(now_utc()), "by": body.regularized_by,
                 "reason": body.reason, "changes": changes}
        n_hist = len(cur.get("history") or [])
        guard: dict = {
            "_id": cur["_id"], "status": cur["status"],
            "punch_in": cur.get("punch_in"), "punch_out": cur.get("punch_out"),  # None also matches missing
        }
        if n_hist:
            guard["history"] = {"$size": n_hist}
        else:
            guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}, {"history": None}]
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return serialize_log(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# ---- analytics ---------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_RE)]):
    start, end = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    pipeline, hint = plan_monthly(emp_code, month)
    rows = list(db.attendance_logs.aggregate(pipeline, **_hint(hint)))
    g = rows[0] if rows else {}
    halves = int(g.get("present_halves", 0))
    working_days = count_working_days(start, end, emp["joined_on"])
    pct = None
    if working_days > 0:
        pct = float(round_half_up(Fraction(halves * 100, 2 * working_days), 2))
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": halves / 2, "leave_days": int(g.get("leave_days", 0)),
        "late_count": int(g.get("late_count", 0)),
        "total_late_minutes": int(g.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(g.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_RE)], department: Optional[str] = None):
    pipeline, hint = plan_department_summary(month, department)
    items = []
    for g in db.employees.aggregate(pipeline, **_hint(hint)):
        n = int(g["work_n"])
        avg = float(round_half_up(Fraction(int(round(g["work_cents"])), 100 * n), 2)) if n else None
        items.append({
            "department": g["_id"], "headcount": int(g["headcount"]),
            "present_days": int(g["present_halves"]) / 2, "avg_work_hours": avg,
            "late_count": int(g["late_count"]), "total_late_minutes": int(g["total_late_minutes"]),
            "leave_count": int(g["leave_count"]), "on_duty_count": int(g["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_RE)],
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)
    pipeline, hint = plan_late_leaderboard(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, **_hint(hint)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[str, Query(alias="from")],
    date_to: Annotated[str, Query(alias="to")],
):
    d_from, d_to = parse_date(date_from, "from"), parse_date(date_to, "to")
    if d_to < d_from:
        invalid("to", "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        invalid("to", f"range must not exceed {MAX_TREND_DAYS} days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline, hint = plan_department_trend(department, d_from, d_to)
    items = []
    for r in db.employees.aggregate(pipeline, **_hint(hint)):
        rate, ma = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]), "present_count": int(r["present_halves"]) / 2,
            "late_count": int(r["late_count"]),
            "attendance_rate": None if rate is None else round_float_half_up(rate, 4),
            "moving_avg_7d": None if ma is None else round_float_half_up(ma, 4),
        })
    return {"department": department, "items": items}


# ---- admin -------------------------------------------------------------------
def _explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**params):
    for name, value in params.items():
        if value is None:
            invalid(name, "field required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch uses the same plan_* builder as the real endpoint, so the explained query is the real one.
    if endpoint == "attendance_list":
        if date_from is not None:
            parse_date(date_from, "date_from")
        if date_to is not None:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            invalid("date_from", "date_from must not be after date_to")
        p = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": p["filter"], "sort": p["sort"], "skip": p["skip"],
               "limit": p["limit"], "hint": p["hint"]}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            month_bounds(month)
            pipeline, hint = plan_monthly(emp_code, month)
            coll = "attendance_logs"
        elif endpoint == "department_summary":
            _require(month=month)
            month_bounds(month)
            pipeline, hint = plan_department_summary(month, department)
            coll = "employees"
        elif endpoint == "late_leaderboard":
            _require(month=month)
            month_bounds(month)
            pipeline, hint = plan_late_leaderboard(month, limit, department)
            coll = "attendance_logs"
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
            if d_to < d_from:
                invalid("to", "to must not be before from")
            if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
                invalid("to", f"range must not exceed {MAX_TREND_DAYS} days")
            pipeline, hint = plan_department_trend(department, d_from, d_to)
            coll = "employees"
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    return {"endpoint": endpoint, "collection": coll, "explain": _explain(cmd)}
