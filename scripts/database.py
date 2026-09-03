"""Database setup for FitAcademie Squash Webapp."""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from contextlib import asynccontextmanager
import os

from models import Base

_raw_db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db")
# Resolve relative paths relative to this file's directory
if _raw_db_url.startswith("sqlite+aiosqlite:///./"):
    _rel = _raw_db_url.replace("sqlite+aiosqlite:///./", "")
    _abs = os.path.join(os.path.dirname(os.path.abspath(__file__)), _rel)
    DATABASE_URL = f"sqlite+aiosqlite:///{_abs}"
else:
    DATABASE_URL = _raw_db_url

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    """FastAPI dependency for DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def db_session():
    """Context manager for standalone scripts."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()