# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne engineering assignment. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Configuration comes from `MONGO_URI` and `MONGO_DB` (defaults: `mongodb://localhost:27017`, `attendance_db`).
A local `.env` file is loaded for convenience, but real environment variables take priority.
All indexes are created at startup and the step is idempotent (see `DECISIONS.md`). Interactive docs: http://localhost:8000/docs

To load the nine sample documents, run the candidate kit's `sample_seed.py` against the same database.

## Notes and decisions

Where the contract left room for interpretation, this is what the code does:

- **Time.** Instants are stored as UTC BSON dates (the client uses `tz_aware=True`), truncated to whole seconds (R1),
  and returned as epoch milliseconds. Shift times and attendance dates are evaluated in IST.
- **Punch-out.** It closes the latest record whose `punch_in` is at or before `punched_at`. If the employee has
  punch-ins but all of them are after `punched_at`, the response is 422 (punch-out must be after punch-in); with no
  punch-in at all it is 404.
- **Regularize (PATCH).** Uses optimistic locking: the update applies only if the record is unchanged since it was
  read (same status, punch times and history length). A concurrent edit gets 409, so no history entry is lost.
  Sending punch times while the record is or becomes `ABSENT`/`LEAVE`, sending `null` for a field, or a request
  that changes nothing all return 422.
- **Monthly `present_days`** counts Mon-Fri presence records in the month as stored. Only `working_days` is counted
  from `joined_on` (R7).
- **Trend.** The 92-day limit counts both ends (1 Jul - 30 Sep is allowed, 93 days is 422). `moving_avg_7d` averages
  the 4-decimal `attendance_rate` values in the window. A department is "unknown" (404) when it has no employees.
- **Rounding** is half-up everywhere (`Decimal` in Python, `Decimal128` plus `$trunc` in MongoDB, because `$round`
  rounds half to even).
- **422 responses** raised by the code use the same `{"detail": [...]}` shape as FastAPI's own validation errors.

## Testing

Tested against the sample data plus extra cases the rules describe: overnight shifts, weekend records, half days,
mid-month joiners, employees with no logs, orphan logs, leaderboard ties, and concurrent punch-in, punch-out and
regularize requests. On a generated dataset of about 100,000 attendance records, every `/admin/explain` plan uses
`IXSCAN` with no `COLLSCAN`, and all endpoints answer in under 300 ms.

`REVIEW.md` lists the defects found in the starter code.
