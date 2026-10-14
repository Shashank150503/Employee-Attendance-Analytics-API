"""Pure unit tests for the business rules (no MongoDB needed):  pytest tests/test_rules.py"""
from datetime import datetime, timedelta, timezone

import pytest

from app.main import (IST, attendance_date, compute_late_minutes, compute_overtime, compute_work_hours,
                      count_working_days, derive, round_float_half_up, round_half_up, truncate_seconds)
from datetime import date


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST).astimezone(timezone.utc)


# R2 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,m,s,expected", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0), (9, 30, 0, 0)])
def test_late_examples_from_spec(h, m, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30") == expected


def test_sub_second_is_truncated_before_grace_check():
    p = truncate_seconds(ist(2026, 7, 6, 9, 40, 0, 900_000))  # 09:40:00.900 counts as 09:40:00
    assert compute_late_minutes(p, "2026-07-06", "09:30") == 0


# R1 ------------------------------------------------------------------------------------------
def test_date_is_ist_not_utc():
    # 23:50 IST on 6 Jul is 18:20 UTC on 6 Jul; 00:10 IST on 7 Jul is 18:40 UTC on 6 Jul
    assert attendance_date(ist(2026, 7, 7, 0, 10), "09:30", "18:30") == "2026-07-07"


def test_overnight_early_morning_punch_in_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert attendance_date(ist(2026, 7, 6, 22, 5), "22:00", "06:00") == "2026-07-06"


# R3 ------------------------------------------------------------------------------------------
def test_overtime_threshold_and_floor():
    assert compute_overtime(ist(2026, 7, 6, 18, 59, 59), "2026-07-06", "09:30", "18:30") == 0   # 29 min
    assert compute_overtime(ist(2026, 7, 6, 19, 0, 0), "2026-07-06", "09:30", "18:30") == 30
    assert compute_overtime(ist(2026, 7, 6, 19, 29, 59), "2026-07-06", "09:30", "18:30") == 59
    assert compute_overtime(ist(2026, 7, 6, 17, 0), "2026-07-06", "09:30", "18:30") == 0


def test_overnight_shift_end_is_next_day():
    assert compute_overtime(ist(2026, 7, 7, 6, 45), "2026-07-06", "22:00", "06:00") == 45


# R4 / R5 -------------------------------------------------------------------------------------
def test_work_hours_half_up_not_bankers():
    # 18 seconds = 0.005 h  -> half-up gives 0.01 (banker's / float rounding gives 0.0)
    base = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(base, base + timedelta(seconds=18)) == 0.01
    # 4.495 h = 16182 s -> 4.50 (half-up) so NOT a half day
    d = derive("09:30", "18:30", "2026-07-06", base, base + timedelta(seconds=16182))
    assert d["work_hours"] == 4.5 and d["half_day"] is False
    d = derive("09:30", "18:30", "2026-07-06", base, base + timedelta(seconds=16178))  # 4.4939 -> 4.49
    assert d["work_hours"] == 4.49 and d["half_day"] is True


# R7 / R8 -------------------------------------------------------------------------------------
def test_working_days():
    jul = (date(2026, 7, 1), date(2026, 7, 31))
    assert count_working_days(*jul, "2026-01-05") == 23
    assert count_working_days(*jul, "2026-07-13") == 15   # joined mid-month: counted from joined_on
    assert count_working_days(*jul, "2026-08-01") == 0


def test_rounding_helpers():
    assert float(round_half_up(__import__("fractions").Fraction(5, 2) / 100, 2)) == 0.03
    assert round_float_half_up(0.31245000000000003, 4) == 0.3125
    assert round_float_half_up(0.3124499, 4) == 0.3124
    assert round_float_half_up(0.0, 4) == 0.0
