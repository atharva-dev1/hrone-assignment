# HROne Employee Attendance & Analytics API

FastAPI-based REST API for employee attendance tracking with MongoDB backend.

## Setup Instructions

### Prerequisites
- Python 3.11 or higher (tested with Python 3.14)
- MongoDB 6.0+ (free tier MongoDB Atlas works)

### Installation

1. Clone or extract the project

2. Create and activate a virtual environment:
```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment variables:
```bash
cp .env.example .env
```

Edit `.env` with your MongoDB connection:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_candidate_test
```

For MongoDB Atlas, use your connection string:
```env
MONGO_URI=mongodb+srv://username:password@cluster.mongodb.net/
```

### Running the Application

```bash
uvicorn app.main:app --port 8000
```

The API will be available at `http://localhost:8000`

## API Documentation

FastAPI provides interactive documentation:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## Database Setup

### Seeding Sample Data

The project includes sample employee and attendance data in `sample_data/`:

```bash
python sample_seed.py
```

This loads 6 employees and 9 attendance records into MongoDB.

### Collections

The application uses two collections:

- `employees`: Employee information with shift schedules
- `attendance_logs`: Attendance records with punch times

MongoDB indexes are created automatically at startup.

## Implemented Endpoints

### System
- `GET /health` - Health check (200/503)

### Employees
- `POST /employees` - Create employee (201/409/422)
- `GET /employees` - List employees with pagination (200/422)

### Attendance
- `POST /attendance/punch-in` - Record punch-in (201/404/409/422)
- `POST /attendance/punch-out` - Record punch-out (200/404/409/422)
- `GET /attendance` - List attendance records (200/422)
- `PATCH /attendance/{emp_code}/{date}` - Regularize attendance (200/404/409/422)

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Monthly employee summary (200/404/422)
- `GET /analytics/departments/summary` - Department summary (200/422)
- `GET /analytics/leaderboard/late` - Late-comers leaderboard (200/422)
- `GET /analytics/departments/{department}/trend` - Daily attendance trend (200/404/422)

### Admin
- `GET /admin/explain/{endpoint}` - MongoDB explain output (200/422)

## Testing

Run the test suite:
```bash
pytest tests/
```

Tests cover:
- All API endpoints (success and error cases)
- Pagination and filtering
- Timezone handling
- Derived field calculations
- Race condition prevention
- Analytics aggregations

## Business Rules Implemented

- **R1**: IST timezone (UTC+5:30), overnight shift handling
- **R2**: Late calculation (10-min grace period, floor minutes)
- **R3**: Overtime calculation (>= 30 min threshold)
- **R4**: Work hours (half-up rounding to 2 decimals)
- **R5**: Half day flag (< 4.50 hours)
- **R6**: Presence status definitions
- **R7**: Working days (Mon-Fri, from joined_on)
- **R8**: Rounding (2 decimals for values, 4 decimals for rates)
- **R9**: Headcount calculation
- **R10**: Pagination (page starts at 1, max 100, default 20)

## Submission Files

This repository contains all required submission files:

- `app/main.py` - API implementation
- `requirements.txt` - Python dependencies
- `REVIEW.md` - Code review findings and fixes
- `DECISIONS.md` - Design decisions and trade-offs
- `README.md` - This documentation

## Known Limitations

- No authentication/authorization layer (as per assignment spec)
- No rate limiting (as per assignment spec)
- Tests require MongoDB connection (integration tests)

## Git Repository

To push this project to GitHub:

```bash
git init
git add .
git commit -m "Initial commit: HROne Employee Attendance API"
git branch -m main
git remote add origin https://github.com/yourusername/hrone-attendance-api.git
git push -u origin main
```

Ensure `.env` is not committed (it's in `.gitignore`).

## License

HROne Engineering Assignment
