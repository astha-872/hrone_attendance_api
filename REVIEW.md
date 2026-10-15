# Starter code review

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Naive datetimes and shift-day assumptions make timezone/overnight calculations unreliable. | Test an overnight shift punch-in before shift end, and a punch-in 10:40:01 after a 09:30 shift. | Convert instants to IST, select the correct shift start date, and apply the strict 10-minute grace rule. |
| 2 | `compute_overtime` | It always uses the attendance date and does not move shift end to the next day for overnight shifts. | Test a 22:00–06:00 shift punched out at 06:40 the next morning. | Determine whether the shift is overnight and calculate the correct shift-end instant. |
| 3 | `create_employee` | Check-then-insert can create duplicate employee codes under concurrent requests; created_at is a naive datetime. | Send concurrent POST requests with the same emp_code. | Enforce a unique database index, catch `DuplicateKeyError`, and store a UTC-aware timestamp. |
| 4 | `list_employees` | Skip is `page * page_size`, total ignores the department filter, and results are not sorted. | Request page 1 and a filtered department. | Use `(page - 1) * page_size`, count the filtered query, and sort by emp_code. |
| 5 | `punch_in` | Unknown employees cause a server error; timestamp is interpreted in machine-local time; check-then-insert races. | Punch in for an unknown code or submit simultaneous duplicate requests. | Return 404, parse epoch milliseconds as UTC, calculate the IST attendance date, and rely on the unique compound index. |
| 6 | `list_attendance` | It loads all matching records into memory before pagination and does not validate the date range. | Use a large collection and request a later page or reversed dates. | Count and page in MongoDB, validate inclusive date filters, and sort consistently. |
| 7 | Whole API | No startup indexes or MongoDB readiness check existed. | Inspect indexes or stop MongoDB and call `/health`. | Create idempotent indexes at startup and ping MongoDB in the health endpoint. |
| 8 | Missing endpoints | Punch-out, regularization, analytics, and explain endpoints were absent. | Call each path in the OpenAPI contract. | Implemented handlers and aggregation pipelines in `app/main.py`. |

Reviewed and retained: the natural attendance key is `(emp_code, date)`; stored derived values are the source of truth for analytics; history is reserved for manual corrections, not punch events.
