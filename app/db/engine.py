import asyncio

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import database_path

# The SQLite file comes from app.config (NEWSFLOW_DB_PATH override, or
# <project root>/newsflow.db) so the engine and the workspace paths in
# app.utils — public/<db name>/… — always agree on the active database.
DB_PATH = database_path()

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
