"""
Pytest configuration and fixtures for Foxway Doorbell backend tests.
Strictly isolated test database so production DB (/mnt/opslag/deurbel/foxway.db)
is NEVER touched or polluted by tests.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

for p in ["/app", "/mnt/opslag/deurbel/backend"]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Create isolated test DB before importing app modules
_test_db_file = tempfile.NamedTemporaryFile(prefix="foxway_test_", suffix=".db", delete=False)
_test_db_path = Path(_test_db_file.name)
_test_db_file.close()

# Force override DB_PATH in both config and models
import app.config as app_config
import app.models as app_models
app_config.settings.DB_PATH = _test_db_path
app_models._DB_PATH = _test_db_path

from app.main import app
from app.models import init_db


@pytest.fixture(autouse=True)
async def setup_db():
    app_config.settings.DB_PATH = _test_db_path
    app_models._DB_PATH = _test_db_path
    await init_db()
    yield


@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
