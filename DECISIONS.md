# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` makes duplicate punch-ins impossible and serves one employee's records and monthly summary. `(date desc, emp_code)` serves the attendance list sort and the leaderboard's month range. `(status, date desc, emp_code)` serves status filters. `(emp_code, punch_in desc)` finds the record to punch out. On employees: unique `emp_code`, `(department, joined_on)` for headcount, `(department, emp_code)` for sorted department lists. Rejected: an index on `late_minutes`; a month holds few rows, so it adds write cost for nothing.
2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. MongoDB's unique index lets exactly one insert win; the other raises `DuplicateKeyError`, which I turn into 409. There is no read before the write, so nothing can slip between them.
3. **Ties.** The leaderboard uses `$rank` after sorting, then keeps `rank <= limit`. All tied rows at the cutoff stay, so more than `limit` rows can return. Cutting by position would drop an employee for no reason.
4. **Headcount.** The summary starts from `employees`, not from logs, filtered by `joined_on <= month end`. Logs are joined with `$lookup`, which keeps employees with an empty result.
5. **At 100x.** Precompute daily per-department counters, updated on punch events, instead of scanning logs per request.
