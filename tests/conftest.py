"""Test configuration for HROne API tests."""
import os
import pytest
from fastapi.testclient import TestClient
from app.main import app, client, db, MONGO_DB, create_indexes

# Use test database
TEST_MONGO_DB = "hrone_candidate_test"

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    """Set up test database before tests."""
    os.environ["MONGO_DB"] = TEST_MONGO_DB
    from app import main
    main.db = client[TEST_MONGO_DB]
    create_indexes()
    yield
    # Clean up after tests
    db.employees.drop()
    db.attendance_logs.drop()

@pytest.fixture
def seeded_db():
    """Seed the test database with sample data."""
    # Use sample_seed.py to load data
    import subprocess
    result = subprocess.run(
        ["python", "sample_seed.py"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0, f"Failed to seed database: {result.stderr}"
    yield

@pytest.fixture
def test_client():
    """Create a test client."""
    yield TestClient(app)
