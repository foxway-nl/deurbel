"""
Pytest configuration and fixtures for Foxway Doorbell backend tests.
"""

import asyncio
import os
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import init_db



@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()
    yield

@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
