# DECISIONS.md

### 1. Indexes
I created a unique index on `emp_code` in the employees collection to prevent duplicate employees. In attendance_logs, I created a unique compound index on `emp_code` and `date` to ensure one attendance record per employee per day. I also considered additional indexes for department and date-based analytics, but would add them only after checking query plans and workload.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially check that no record exists. The unique compound index ensures that only one insert succeeds. The other request receives a duplicate-key error, which the API should handle gracefully rather than creating a second record.

### 3. Ties
At the leaderboard limit, employees tied on late minutes may otherwise be ordered unpredictably. I use a secondary sort, such as `emp_code`, to make the result deterministic. The leaderboard returns exactly the requested limit rather than automatically including every tied employee.

### 4. Headcount
The department summary starts from employees, groups them by department, and then looks up attendance logs. This keeps employees with no attendance records in the summary, so they still contribute to headcount.

### 5. Scaling to 100x data
I would inspect execution plans, add indexes based on actual query patterns, and benchmark aggregation pipelines. I would also consider pagination and monitoring slow queries to keep response times manageable.