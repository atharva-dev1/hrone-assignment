"""Test configuration for HROne API tests."""
import os
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.main import app, client


@pytest.fixture
def setup_test_db(monkeypatch):
    """Give each test a disposable database, separate from the configured database."""
    from app import main
    test_db_name = f"hrone_test_{uuid4().hex[:16]}"
    original_db = main.db
    monkeypatch.setenv("MONGO_DB", test_db_name)
    main.db = client[test_db_name]
    try:
        yield
    finally:
        try:
            client.drop_database(test_db_name)
        finally:
            main.db = original_db


@pytest.fixture
def seeded_db(setup_test_db):
    """Seed the test database with sample data."""
    # Use sample_seed.py to load data
    import subprocess
    result = subprocess.run(
        [sys.executable, "sample_seed.py"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0, f"Failed to seed database: {result.stderr}"
    yield


@pytest.fixture
def test_client(setup_test_db):
    """Create a test client."""
    with TestClient(app) as client:
        yield client
