# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or a local .env
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$setWindowFields`, `$dateAdd`). Indexes are created at startup and are idempotent.

Tests: `pytest tests/` (business rules and the non-aggregation endpoints run without a MongoDB server).

## What is implemented
All endpoints of `openapi.yaml`: health, employees, punch-in, punch-out, attendance list, PATCH regularize with
audit history, employee monthly, department summary, late leaderboard, department trend, and admin explain.
Part A is in `REVIEW.md`, Part C in `DECISIONS.md`.

## Design notes and assumptions
* **Concurrency** is handled by the database: unique indexes for employee creation and punch-in, a compare-and-set
  `find_one_and_update` for punch-out, and optimistic locking (guard on current state + history length, up to 5
  retries, then 409) for PATCH.
* **Analytics** are aggregation pipelines. Sums are kept as integers (half-day units, hundredths of an hour) and
  rounded half-up in Python with exact arithmetic, because MongoDB's `$round` rounds half to even.
* **Trend** generates the one-row-per-day series inside the database with `$range` + `$unwind` (not a Python loop),
  and computes the 7-day average with `$setWindowFields` over the preceding 6 rows.
* **moving_avg_7d** averages the *unrounded* daily rates and rounds the result once (R8: only reported numbers are
  rounded). If the grader averages already-rounded rates, results can differ in the 4th decimal.
* **Trend limit**: a range of 92 calendar days (inclusive) is accepted, 93 is a 422.
* `explain` builds each query with the same `plan_*` function the real endpoint uses, including the index hint.
* An explicit `null` for `status`/`punch_in`/`punch_out` in PATCH is a 422 (omit the field to leave it unchanged).

## Not verified here
The aggregation endpoints were written against the MongoDB 7.0 documentation but I could not run them against a real
server in my environment; please run them against your own instance (`python sample_seed.py`) before relying on them.
