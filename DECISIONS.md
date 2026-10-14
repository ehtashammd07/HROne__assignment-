# DECISIONS.md

1. **Indexes.** On `employees`, we created a unique index on `{"emp_code": 1}` to guarantee business-key uniqueness and a compound index on `{"department": 1, "joined_on": 1}` for fast department filtering and headcount evaluation. On `attendance_logs`, we created a unique compound index on `{"emp_code": 1, "date": 1}` to guarantee one record per employee-day and enforce race safety; `{"date": -1, "emp_code": 1}` for indexed pagination; `{"emp_code": 1, "punch_in": -1}` to quickly find the latest punch-in at punch-out; and `{"date": 1, "late_minutes": -1}` for late rankings. We considered and rejected an isolated index on `punch_out` because punch-out lookups are always scoped by `emp_code` and `punch_in`, making a standalone index redundant write overhead.

2. **Punch-in race.** Both requests compute the attendance date and attempt `insert_one`. Because `attendance_logs` enforces a unique index on `(emp_code, date)`, MongoDB atomically accepts the first document and rejects the second with `DuplicateKeyError` (code 11000). Our endpoint catches this error and returns HTTP 409 Conflict, ensuring exactly one 201 without fragile check-then-insert logic.

3. **Ties.** The leaderboard computes standard competition ranking using `$setWindowFields` with `$rank` (1, 2, 2, 4). Because the contract requires returning rows with `rank <= limit`, both tied employees are returned, even if the total row count exceeds `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.** The department summary aggregates from the `employees` collection (matching `joined_on <= month_end`) and groups employees to establish department headcount before joining attendance logs via an outer join (`preserveNullAndEmptyArrays: true`). Employees with zero logs remain in the headcount while contributing zero to logs totals.

5. **One thing you would change for 100x data.** At 100x volume (~10M logs), we would maintain pre-aggregated daily and monthly summaries via an event-driven worker or MongoDB Change Streams upon punch-out and regularization, eliminating costly real-time cross-collection joins and window functions over large date ranges.
