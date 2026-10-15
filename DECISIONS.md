1. **Indexes.** I use a unique `employees.emp_code` index and a unique compound `attendance_logs(emp_code, date)` index. They enforce the two natural-key guarantees even during concurrent requests. Department/join-date, employee/date/status, date/employee, and date/late-minute indexes support filtered lists and analytics. I would check explain output before adding more indexes because each index increases write cost.

2. **Punch-in race.** Both requests may initially see no record, but the unique compound index permits only one insert. The winner returns 201; MongoDB rejects the other insert and the API maps that duplicate-key error to 409.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so equal totals share a rank and the next rank is skipped. Filtering by rank after ranking keeps every employee tied at the cutoff; emp_code breaks display-order ties.

4. **Headcount.** Department reporting starts from employees, not attendance logs, and uses a left-style lookup. This preserves employees without logs; `joined_on <= month end` excludes future joiners.

5. **100x data.** I would benchmark the aggregation plans with representative data, add targeted indexes only where explain shows a benefit, and consider pre-aggregated daily summaries for heavily used analytics.
