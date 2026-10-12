# DECISIONS.md

## 1. Indexes
I created the following indexes at startup (idempotent via existence checks):

**Employees collection:**
- `emp_code_1` (unique): Enforces unique emp_code per contract, supports create_employee race safety and analytics lookups.
- `department_1`: Supports list_employees filter by department and department_summary grouping.
- `joined_on_1`: Supports headcount calculation (R9: joined_on <= period end) in department_summary and employee_monthly.

**Attendance_logs collection:**
- `emp_code_1_date_1` (unique): Race-safe punch-in (exactly one 201 for concurrent duplicates), also supports employee_monthly and punch-out queries.
- `date_-1_emp_code_1`: Covers list_attendance sort (date desc, emp_code asc) and pagination.
- `status_1_date_-1`: Supports list_attendance status filter + date sort.
- `date_1`: Supports month-range queries in analytics (employee_monthly, department_summary, late_leaderboard, department_trend).
- `emp_code_1_date_1_asc`: Supports analytics pipelines that need emp_code + date ascending (e.g., $lookup in department_summary).

I considered a compound index on `(emp_code, status, date)` for list_attendance but rejected it: the existing `date_-1_emp_code_1` + `status_1_date_-1` cover all filter/sort combinations, and adding more indexes increases write overhead without significant read benefit for the 100k scale.

## 2. Punch-in race
Two identical punch-in requests arrive at the same instant for the same emp_code and date. Both pass the employee existence check. Both construct the same document with the same (emp_code, date). The unique index on `emp_code_1_date_1` makes `insert_one` atomic at the database level. The first request wins (201), the second gets `DuplicateKeyError` which we catch and return 409. No check-then-insert race window exists. The response includes the created record with epoch-millisecond timestamps.

## 3. Ties at leaderboard cutoff
Late leaderboard uses MongoDB `$rank` (standard competition ranking: 1, 2, 2, 4). The pipeline sorts by `total_late_minutes` desc, then `emp_code` asc, applies `$rank`, then filters `rank <= limit`. If employees tie at the cutoff rank (e.g., limit=10 but ranks 10, 10, 10 exist), all three are returned because they share rank 10. The response can therefore contain more than `limit` rows. Tied rows are ordered by `emp_code` ascending per the `$sort` before `$rank`.

## 4. Headcount with zero logs
Department summary headcount (R9) counts employees with `joined_on <= month_end` in that department, regardless of whether they have attendance logs. The pipeline does a `$lookup` from attendance_logs to employees, filters by department and joined_on, then groups. However, employees with zero logs in the month would be filtered out by the `$match` on attendance_logs date. To fix this, I restructured: the pipeline starts from employees (filtered by department and joined_on <= month_end) to get the headcount, then `$lookup`s attendance_logs for the month. This ensures zero-log employees are counted in headcount but contribute 0 to present_days, late_count, etc. The `avg_work_hours` averages over records (not employees), so zero-log employees don't affect it.

## 5. One change for 100x data
If this served 100x the data (~10M attendance records), I would:
- Switch from synchronous PyMongo to Motor (async) and make all endpoints `async` to avoid thread-pool starvation under concurrent load.
- Add a Redis cache layer for list_attendance and analytics with short TTLs (e.g., 30s) to absorb read spikes.
- Pre-aggregate daily department metrics into a materialized collection updated by punch-in/out/regularize (change stream or application-level), so department_trend and department_summary read pre-computed daily rows instead of scanning 10M documents.
- Use MongoDB sharding on `date` for attendance_logs and `emp_code` for employees.
- Add connection pooling tuning and circuit breakers for downstream resilience.