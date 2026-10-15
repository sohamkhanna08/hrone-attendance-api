# DECISIONS.md

1. **Indexes.** On `employees`: unique `emp_code` (no duplicates, fast lookups), plus `(department, joined_on)` and `joined_on` for headcount with and without a department filter. On `attendance_logs`: unique `(emp_code, date)` (one record per day, safe punch-in, monthly totals and joins), `(date desc, emp_code)` for the list sort and month ranges, `(status, date desc, emp_code)` for the status filter, and `(emp_code, punch_in desc)` for punch-out. I rejected a separate `department` index because the compound index already starts with it.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` with no check first. The unique index lets only the first insert succeed; the second gets `DuplicateKeyError`, which I return as 409. Twenty parallel requests gave one 201 and nineteen 409s.

3. **Ties.** `$rank` gives equal totals the same rank (1, 2, 2, 4), and `limit` is applied after ranking, so both tied employees are returned and the response can be longer than `limit`.

4. **Headcount.** The pipeline starts from `employees` (joined by month end, R9) and counts per department before joining logs; `preserveNullAndEmptyArrays` keeps departments whose employees have no logs.

5. **100x data.** I would keep a pre-aggregated collection of daily department totals, updated on writes or with `$merge`, so analytics read a few rows instead of millions of logs.
