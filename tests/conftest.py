"""
Shared pytest fixtures.
"""
import asyncio
import pytest
import aiosqlite


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db(tmp_path):
    """In-memory aiosqlite DB with schema applied."""
    from database.db import init_db
    db_path = tmp_path / "test.db"
    conn = await init_db(str(db_path))
    yield conn
    await conn.close()
