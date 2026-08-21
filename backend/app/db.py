from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
# Pool sized for batch-ingestion fan-in: when a provider batch resolves, every
# document in the slice (up to SLICE=2000) briefly wants a connection for its
# short write transaction. The SQLAlchemy defaults (5+10, 30s acquire timeout)
# mass-fail the tail of that stampede — seen 15 Aug, 1223/1223 units lost.
# 40+60 against Postgres max_connections=200 clears a 2000-doc slice with
# a long timeout as the backstop. The structural fix (a bounded write gate in
# the oneshot pipeline) is ticketed for the cloud deployment, where connection
# budgets are tighter.
engine = create_async_engine(
    _settings.sgr_database_url,
    pool_pre_ping=True,
    pool_size=40,
    max_overflow=60,
    pool_timeout=180,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
