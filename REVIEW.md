# REVIEW.md

I reviewed the starter code for correctness, concurrency, pagination, and compliance with the API contract. The following defects were identified and addressed in the implementation.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Reports success without checking MongoDB connectivity. | Stop MongoDB and call `GET /health`; it may still report healthy. | Run a MongoDB `ping` and return HTTP 503 when the database is unavailable. |
| 2 | `compute_late_minutes()` | Incorrect timezone or overnight-shift handling can calculate lateness against the wrong shift date. | Test a punch-in around midnight for an overnight shift. | Convert timestamps to IST, use the attendance date, and apply the 10-minute grace period. |
| 3 | `compute_work_hours()` | Standard rounding may not follow the required half-up rounding rule. | Test durations near a two-decimal rounding boundary. | Truncate timestamps to whole seconds and use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime()` | Overnight shifts can use the wrong shift-end instant. | Test punch-out after midnight for a shift ending the next day. | Calculate the shift end using the attendance date and overnight-shift rules; award overtime only from 30 minutes onward. |
| 5 | `create_employee()` | Checking for an existing employee before inserting is vulnerable to concurrent requests. | Send two requests with the same `emp_code` simultaneously. | Create a unique index on `emp_code` and handle `DuplicateKeyError` with HTTP 409. |
| 6 | `list_employees()` | Incorrect pagination, unstable ordering, or a total that ignores filters can produce inconsistent results. | Request later pages and compare totals for a department filter. | Apply the department filter to the count, sort by `emp_code`, and use `(page - 1) * page_size` for the offset. |
| 7 | `punch_in()` | Missing employee validation or a check-before-insert approach can cause incorrect errors or duplicate attendance records. | Punch in for an unknown employee or submit simultaneous duplicate requests. | Return HTTP 404 for an unknown employee; enforce uniqueness on `(emp_code, date)` and handle duplicate-key errors with HTTP 409. |
| 8 | `list_attendance()` | Fetching records before sorting and paginating in the database is inefficient; raw MongoDB fields can also violate the response contract. | Use a large dataset or inspect responses for `_id` and datetime values. | Apply filtering, sorting, skipping, and limiting in MongoDB, exclude `_id`, and convert timestamps to epoch milliseconds. |

## Reviewed but not considered defects

- **MongoDB unique indexes:** These are necessary for enforcing uniqueness under concurrent requests; application-level checks alone are insufficient.
- **IST date handling:** Converting UTC instants to IST for attendance-date decisions is intentional and required by the business rules.
- **Analytics aggregation pipelines:** Computing analytics in MongoDB is appropriate for larger datasets, provided the pipeline follows the specified counting and rounding rules.
- **Pagination limits:** A default page size of 20 and a maximum of 100 are intentional API constraints, not defects.