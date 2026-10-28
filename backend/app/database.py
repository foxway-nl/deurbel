"""
Foxway Smart Doorbell — Centralized Robust SQLite Concurrency Manager.

Fixes SQLite `database is locked` operational errors:
- Forces WAL journal mode: PRAGMA journal_mode=WAL;
- Sets PRAGMA synchronous=NORMAL;
- Sets PRAGMA busy_timeout=60000; (60s wait for locks)
- Sets connection timeout=60.0 and check_same_thread=False.
- Protects concurrent write transactions with a per-loop asyncio.Lock and threading.RLock.
- Guarantees clean connection teardown in try...finally blocks.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Generator

import aiosqlite

from app.config import settings

logger = logging.getLogger("foxway.database")

_loop_write_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_sync_write_lock = threading.RLock()


def get_db_path() -> str:
    return str(settings.DB_PATH)


def get_async_write_lock() -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.Lock()
    if loop not in _loop_write_locks:
        _loop_write_locks[loop] = asyncio.Lock()
    return _loop_write_locks[loop]


def init_wal_mode() -> None:
    """Synchronously initializes WAL mode and PRAGMAs on DB file at process start."""
    try:
        with sqlite3.connect(get_db_path(), timeout=60.0, check_same_thread=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=60000;")
            logger.info("SQLite initialized with PRAGMA journal_mode=WAL, busy_timeout=60000 on %s", get_db_path())
    except Exception as e:
        logger.error("Failed to initialize SQLite WAL mode: %s", e)


@asynccontextmanager
async def get_db(write: bool = False) -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Async context manager for SQLite connections.
    If write=True, synchronizes execution using event loop write lock to avoid concurrent write locks.
    """
    lock_acquired = False
    lock = None
    if write:
        lock = get_async_write_lock()
        await lock.acquire()
        lock_acquired = True

    db = None
    try:
        db = await aiosqlite.connect(get_db_path(), timeout=60.0, check_same_thread=False)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=60000;")

        yield db

        if write:
            await db.commit()
    except Exception as e:
        if write and db:
            try:
                await db.rollback()
            except Exception:
                pass
        logger.error("SQLite error (write=%s): %s", write, e)
        raise
    finally:
        if db:
            try:
                await db.close()
            except Exception:
                pass
        if lock_acquired and lock:
            lock.release()


@contextmanager
def get_sync_db(write: bool = False) -> Generator[sqlite3.Connection, None, None]:
    """
    Synchronous context manager for SQLite connections in worker threads.
    If write=True, synchronizes execution using _sync_write_lock.
    """
    lock_acquired = False
    if write:
        _sync_write_lock.acquire()
        lock_acquired = True

    conn = None
    try:
        conn = sqlite3.connect(get_db_path(), timeout=60.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")

        yield conn

        if write:
            conn.commit()
    except Exception as e:
        if write and conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("Synchronous SQLite error (write=%s): %s", write, e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        if lock_acquired:
            _sync_write_lock.release()
