"""HTTP-level tests for the non-aggregation endpoints, run against mongomock (no MongoDB needed).
The aggregation endpoints use $setWindowFields / $facet etc. which mongomock cannot execute, so those must be
checked against a real MongoDB (see README).   pytest tests/test_flow.py"""
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m

IST_OFFSET_MS = 5.5 * 3600 * 1000


def ms(y, mo, d, h, mi, s=0):  # an IST wall-clock time as epoch ms
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000 - IST_OFFSET_MS)


@pytest.fixture()
def api(monkeypatch):
    fake = mongomock.MongoClient(tz_aware=True)["t"]
    monkeypatch.setattr(m, "db", fake)
    monkeypatch.setattr(m, "_hint", lambda name: {})
    m.ensure_indexes()
    c = TestClient(m.app)
    base = {"name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05"}
    assert c.post("/employees", json={**base, "emp_code": "EMP0001"}).status_code == 201
    assert c.post("/employees", json={**base, "emp_code": "EMP0002", "shift_start": "22:00", "shift_end": "06:00"}).status_code == 201
    return c


def test_employee_validation_and_duplicates(api):
    good = {"emp_code": "EMP0009", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-02-30"}
    assert api.post("/employees", json=good).status_code == 422                       # impossible date
    good["joined_on"] = "2026-02-27"
    assert api.post("/employees", json={**good, "shift_start": "09:30", "shift_end": "09:30"}).status_code == 422
    assert api.post("/employees", json={**good, "emp_code": "X1"}).status_code == 422
    r = api.post("/employees", json=good)
    assert r.status_code == 201 and "_id" not in r.json() and isinstance(r.json()["created_at"], int)
    assert api.post("/employees", json=good).status_code == 409


def test_list_employees_pagination_and_total(api):
    r = api.get("/employees", params={"page": 1, "page_size": 1}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001"] and r["total"] == 2
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422
    assert api.get("/employees", params={"department": "eng"}).json()["total"] == 0   # case-sensitive


def test_punch_in_rules(api):
    t = ms(2026, 7, 6, 9, 40, 1)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["history"] == [] and j["punch_in"] == t // 1000 * 1000
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE1234"}).status_code == 404
    for bad in (t // 1000, float(t), str(t), True):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422


def test_overnight_punch_out_across_midnight(api):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 22, 0)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 7, 6, 45)})
    j = r.json()
    assert r.status_code == 200 and j["work_hours"] == 8.75 and j["overtime_minutes"] == 45 and j["half_day"] is False
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 7, 7, 0)}).status_code == 409


def test_punch_out_errors(api):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0, 1)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 12, 0)}).json()["half_day"] is True


def test_regularize_audit_trail(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 5)})
    url = "/attendance/EMP0001/2026-07-06"
    body = {"punch_in": ms(2026, 7, 6, 9, 0), "reason": "forgot to punch", "regularized_by": "hr1", "late_minutes": 99}
    r = api.patch(url, json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "work_hours", "late_minutes", "overtime_minutes"} or "punch_in" in ch
    assert ch["late_minutes"] == {"from": 35, "to": 0} and isinstance(ch["punch_in"]["from"], int)
    assert api.patch(url, json=body).status_code == 422                       # nothing changes now
    r = api.patch(url, json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr1"})
    j = r.json()
    assert j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
    assert api.patch(url, json={"status": "LEAVE", "punch_in": ms(2026, 7, 6, 9, 0), "reason": "xxxxx", "regularized_by": "h"}).status_code == 422
    assert api.patch(url, json={"status": "PRESENT", "reason": "xxxxx", "regularized_by": "h"}).status_code == 422   # needs punch_in
    assert api.patch("/attendance/EMP0001/2026-07-07", json=body).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-01", json=body).status_code == 422
    r = api.patch(url, json={"status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 0), "reason": "xxxxx", "regularized_by": "h"})
    assert r.status_code == 422                                                # other attendance date
