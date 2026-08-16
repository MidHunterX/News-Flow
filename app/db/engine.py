import asyncio
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Default to <project root>/newsflow.db; override with NEWSFLOW_DB_PATH for
# tests or alternate environments.
DB_PATH = Path(
    os.environ.get("NEWSFLOW_DB_PATH")
    or Path(__file__).resolve().parent.parent.parent / "newsflow.db"
)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    """Apply per-connection pragmas (WAL + a write busy timeout)."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


async def run_in_thread(func, *args, **kwargs):
    """Run a blocking DB call in a worker thread."""
    return await asyncio.to_thread(func, *args, **kwargs)
