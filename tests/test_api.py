"""Test cases for HROne API."""
import pytest
from fastapi.testclient import TestClient
from app.main import app, client, db

TEST_MONGO_DB = "hrone_candidate_test"


class TestHealth:
    """Test health endpoint."""
    
    def test_health_ok(self, test_client):
        """Test health returns ok when MongoDB is available."""
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


class TestEmployees:
    """Test employee endpoints."""
    
    def test_create_employee(self, test_client):
        """Test creating a new employee."""
        emp_data = {
            "emp_code": "EMP1001",
            "name": "Test Employee",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        }
        response = test_client.post("/employees", json=emp_data)
        assert response.status_code == 201
        data = response.json()
        assert data["emp_code"] == "EMP1001"
        assert data["name"] == "Test Employee"
    
    def test_create_duplicate_employee(self, test_client):
        """Test creating duplicate employee returns 409."""
        emp_data = {
            "emp_code": "EMP1002",
            "name": "Duplicate Test",
            "email": "dup@example.com",
            "department": "Engineering"
        }
        test_client.post("/employees", json=emp_data)
        response = test_client.post("/employees", json=emp_data)
        assert response.status_code == 409
    
    def test_list_employees_empty(self, test_client):
        """Test listing employees when database is empty."""
        response = test_client.get("/employees")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
    
    def test_list_employees_with_filter(self, test_client, seeded_db):
        """Test listing employees with department filter."""
        response = test_client.get("/employees?department=Engineering")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for emp in data["items"]:
            assert emp["department"] == "Engineering"
    
    def test_list_employees_pagination(self, test_client, seeded_db):
        """Test pagination works correctly."""
        response = test_client.get("/employees?page=1&page_size=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) <= 2
        assert data["total"] > 0


class TestPunchIn:
    """Test punch-in endpoint."""
    
    def test_punch_in_success(self, test_client, seeded_db):
        """Test successful punch-in."""
        punch_in_data = {
            "emp_code": "EMP0001",
            "status": "PRESENT"
        }
        response = test_client.post("/attendance/punch-in", json=punch_in_data)
        assert response.status_code == 201
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] is not None
        assert data["punch_out"] is None
    
    def test_punch_in_unknown_employee(self, test_client):
        """Test punch-in for unknown employee returns 404."""
        punch_in_data = {
            "emp_code": "EMP9999"
        }
        response = test_client.post("/attendance/punch-in", json=punch_in_data)
        assert response.status_code == 404
    
    def test_punch_in_duplicate(self, test_client, seeded_db):
        """Test duplicate punch-in returns 409."""
        punch_in_data = {"emp_code": "EMP0001"}
        test_client.post("/attendance/punch-in", json=punch_in_data)
        response = test_client.post("/attendance/punch-in", json=punch_in_data)
        assert response.status_code == 409


class TestPunchOut:
    """Test punch-out endpoint."""
    
    def test_punch_out_success(self, test_client, seeded_db):
        """Test successful punch-out."""
        punch_in_data = {"emp_code": "EMP0006"}
        test_client.post("/attendance/punch-in", json=punch_in_data)
        
        punch_out_data = {"emp_code": "EMP0006"}
        response = test_client.post("/attendance/punch-out", json=punch_out_data)
        assert response.status_code == 200
        data = response.json()
        assert data["punch_out"] is not None
        assert data["work_hours"] is not None
        assert data["work_hours"] > 0
    
    def test_punch_out_unknown_employee(self, test_client):
        """Test punch-out for unknown employee returns 404."""
        punch_out_data = {"emp_code": "EMP9999"}
        response = test_client.post("/attendance/punch-out", json=punch_out_data)
        assert response.status_code == 404
    
    def test_punch_out_no_punch_in(self, test_client, seeded_db):
        """Test punch-out without punch-in returns 404."""
        punch_out_data = {"emp_code": "EMP0002"}
        response = test_client.post("/attendance/punch-out", json=punch_out_data)
        assert response.status_code == 404


class TestAttendanceList:
    """Test attendance listing endpoint."""
    
    def test_list_attendance(self, test_client, seeded_db):
        """Test listing attendance records."""
        response = test_client.get("/attendance")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
    
    def test_list_attendance_with_filters(self, test_client, seeded_db):
        """Test attendance listing with filters."""
        response = test_client.get("/attendance?emp_code=EMP0001")
        assert response.status_code == 200
        data = response.json()
        for record in data["items"]:
            assert record["emp_code"] == "EMP0001"
    
    def test_list_attendance_invalid_date_range(self, test_client, seeded_db):
        """Test invalid date range returns 422."""
        response = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        assert response.status_code == 422


class TestRegularizeAttendance:
    """Test attendance regularization endpoint."""
    
    def test_regularize_success(self, test_client, seeded_db):
        """Test successful attendance regularization."""
        regularize_data = {
            "status": "WFH",
            "reason": "Remote work approved",
            "regularized_by": "hr.admin"
        }
        response = test_client.patch("/attendance/EMP0001/2026-07-06", json=regularize_data)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "WFH"
        assert len(data["history"]) == 1
    
    def test_regularize_unknown_employee(self, test_client):
        """Test regularization for unknown employee returns 404."""
        regularize_data = {
            "status": "WFH",
            "reason": "Test",
            "regularized_by": "hr.admin"
        }
        response = test_client.patch("/attendance/EMP9999/2026-07-06", json=regularize_data)
        assert response.status_code == 404
    
    def test_regularize_invalid_status(self, test_client, seeded_db):
        """Test regularization with invalid status combination returns 422."""
        regularize_data = {
            "status": "LEAVE",
            "punch_in": 1783312500000,
            "reason": "Test",
            "regularized_by": "hr.admin"
        }
        response = test_client.patch("/attendance/EMP0001/2026-07-06", json=regularize_data)
        assert response.status_code == 422


class TestEmployeeMonthly:
    """Test employee monthly analytics endpoint."""
    
    def test_employee_monthly_success(self, test_client, seeded_db):
        """Test successful monthly analytics."""
        response = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert response.status_code == 200
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert "working_days" in data
        assert "present_days" in data
    
    def test_employee_monthly_unknown(self, test_client):
        """Test analytics for unknown employee returns 404."""
        response = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert response.status_code == 404


class TestDepartmentSummary:
    """Test department summary analytics endpoint."""
    
    def test_department_summary_success(self, test_client, seeded_db):
        """Test successful department summary."""
        response = test_client.get("/analytics/departments/summary?month=2026-07")
        assert response.status_code == 200
        data = response.json()
        assert "month" in data
        assert "items" in data
        for dept in data["items"]:
            assert "department" in dept
            assert "headcount" in dept


class TestLateLeaderboard:
    """Test late leaderboard analytics endpoint."""
    
    def test_late_leaderboard_success(self, test_client, seeded_db):
        """Test successful late leaderboard."""
        response = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert response.status_code == 200
        data = response.json()
        assert "month" in data
        assert "items" in data
        for entry in data["items"]:
            assert "rank" in entry
            assert "total_late_minutes" in entry


class TestDepartmentTrend:
    """Test department trend analytics endpoint."""
    
    def test_department_trend_success(self, test_client, seeded_db):
        """Test successful department trend."""
        response = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31")
        assert response.status_code == 200
        data = response.json()
        assert "department" in data
        assert "items" in data
        assert len(data["items"]) > 0
    
    def test_department_trend_unknown_dept(self, test_client):
        """Test trend for unknown department returns 404."""
        response = test_client.get("/analytics/departments/Unknown/trend?from=2026-07-01&to=2026-07-31")
        assert response.status_code == 404


class TestAdminExplain:
    """Test admin explain endpoint."""
    
    def test_explain_attendance_list(self, test_client):
        """Test explain for attendance list."""
        response = test_client.get("/admin/explain/attendance_list?page=1&page_size=10")
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "attendance_list"
        assert "explain" in data
    
    def test_explain_employee_monthly(self, test_client):
        """Test explain for employee monthly."""
        response = test_client.get("/admin/explain/employee_monthly?month=2026-07")
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "employee_monthly"
    
    def test_explain_missing_params(self, test_client):
        """Test explain with missing required params returns 422."""
        response = test_client.get("/admin/explain/employee_monthly")
        assert response.status_code == 422
