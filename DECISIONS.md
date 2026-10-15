# DECISIONS.md

### 1. Indexes
I created a unique index on emp_code in the employees collection to prevent duplicate employees. In attendance_logs, I created a unique compound index on emp_code and date to ensure one attendance record per employee per day. I also created indexes for department and joining date, attendance date sorting, employee/date/status filtering, and recent punch-ins. I would consider additional indexes only after checking query plans and workload.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially check that no record exists. The unique compound index ensures that only one insert succeeds. The other request raises a duplicate-key error, which the API handles by returning HTTP 409 Conflict.

### 3. Ties
The leaderboard uses MongoDB's $rank to rank employees by total late minutes. Employees tied at the cutoff receive the same rank, so the response may contain more employees than the requested limit. A secondary sort by emp_code makes the result order deterministic.

### 4. Headcount
The department summary starts from employees who joined on or before the month's end and then looks up their attendance logs. Employees with no attendance records remain in the aggregation and still contribute to headcount.

### 5. Scaling to 100x data
I would inspect execution statistics, benchmark aggregation pipelines, and tune indexes based on actual query patterns. I would also optimize lookups to reduce unnecessary documents being processed.