# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, attendance regularization, and analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo

## Features

- Create and list employees with pagination.
- Record employee punch-in and punch-out.
- List attendance records with filters and pagination.
- Regularize attendance records with change history.
- Generate monthly employee attendance analytics.
- Generate department summaries, late-arrival leaderboards, and department trends.
- Inspect MongoDB query execution plans.

## Project Structure

```text
employee-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
├── DATA_MODEL.md
├── DECISIONS.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Local Setup

### 1. Create and activate a virtual environment

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure MongoDB

Ensure MongoDB 6.0 or newer is running locally. Configure these environment variables as needed:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Keep your `.env` file private and never commit secrets.

### 4. Load sample data (optional)

```powershell
python sample_seed.py
```

**Warning:** The seed script clears the existing `employees` and `attendance_logs` collections before inserting sample records. Do not run it against data you need to preserve.

### 5. Start the API

```powershell
uvicorn app.main:app --port 8000
```

The API documentation is available at:

- Swagger UI: http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Documentation

- `openapi.yaml` — API contract and business rules.
- `DATA_MODEL.md` — MongoDB data model.
- `REVIEW.md` — Starter-code defect review and fixes.
- `DECISIONS.md` — Indexing, concurrency, ranking, and scalability decisions.

## Assignment Submission

This repository contains the implementation and supporting documentation for the Employee Attendance & Analytics API assignment.