"""
Database session management.

This module provides two ways to interact with the database:

  1. ASYNC sessions — used by the FastAPI web server.
     FastAPI is an async framework, so blocking DB calls would freeze the
     event loop and prevent handling other requests. The async engine uses
     the `aiosqlite` driver to make DB calls non-blocking.

  2. SYNC sessions — used by data-loading scripts (download, parse, agents).
     Scripts run as standalone processes where there's no event loop to block.
     Sync code is simpler to write and debug for one-shot batch operations.

Both engines point to the same SQLite file, so data written by scripts
is immediately visible to the API server (after the script commits).

Design note: We use `expire_on_commit=False` on async sessions so that
ORM objects remain usable after commit without triggering lazy-load errors.
This is especially important in async code where lazy loads would fail.
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from typing import AsyncGenerator

from backend.config import settings
from backend.db.models import Base


# =============================================================================
# Async Engine (FastAPI)
# =============================================================================

async_engine = create_async_engine(
    settings.database_url,
    echo=False,  # Set True to see SQL queries in logs (noisy but helpful for debugging)
    # SQLite requires this flag to allow multi-threaded access.
    # Without it, FastAPI's thread pool would trigger "SQLite objects created
    # in a thread can only be used in that same thread" errors.
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Prevents lazy-load issues after commit in async context
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a database session per request.

    Usage in a route:
        @app.get("/bridges")
        async def list_bridges(db: AsyncSession = Depends(get_db)):
            ...

    The session is automatically closed when the request ends,
    even if an exception occurs (the `async with` handles cleanup).
    """
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """Create all database tables from the ORM models (async version)."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Database tables created.")


# =============================================================================
# Sync Engine (Scripts & Agents)
# =============================================================================

sync_engine = create_engine(
    settings.database_url_sync,
    echo=False,
    connect_args={"check_same_thread": False},
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,   # Don't auto-flush; we control when writes happen
    autocommit=False,  # Require explicit commit() — safer for bulk operations
)


def get_sync_db() -> Session:
    """
    Create a sync database session for use in scripts.

    Caller is responsible for closing the session:
        db = get_sync_db()
        try:
            ... do work ...
            db.commit()
        finally:
            db.close()
    """
    return SyncSessionLocal()


def init_db_sync():
    """Create all database tables from the ORM models (sync version for scripts)."""
    Base.metadata.create_all(bind=sync_engine)
    print("✅ Database tables created.")
