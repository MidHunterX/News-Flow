"""Table creation, schema migrations, and startup initialization."""

from datetime import datetime, timezone

from sqlalchemy import delete, inspect, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.constants import DEFAULT_SETTINGS, LAST_RUN_DATE_KEY
from app.db.engine import Base, SessionLocal, engine
from app.db.models import Article, Setting


def _migrate() -> None:
    """Add columns introduced after the initial schema to existing databases."""
    columns = {col["name"] for col in inspect(engine).get_columns("articles")}
    with engine.begin() as conn:
        if "status" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN status TEXT"))
        if "accepted_at" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN accepted_at TEXT"))
        if "accepted_order" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN accepted_order INTEGER"))


def init_db() -> None:
    """Create the tables/indexes (and migrate) if they don't exist."""
    Base.metadata.create_all(engine)
    _migrate()

    with SessionLocal() as session:
        for key, value in DEFAULT_SETTINGS.items():
            session.execute(
                sqlite_insert(Setting)
                .values(key=key, value=value)
                .on_conflict_do_nothing()
            )
        # Fresh workspace per day: clear the articles table when the last run
        # was on a previous day.
        today = datetime.now(timezone.utc).date().isoformat()
        last_run = session.get(Setting, LAST_RUN_DATE_KEY)
        if last_run is None or last_run.value != today:
            session.execute(delete(Article))
            if last_run is None:
                session.add(Setting(key=LAST_RUN_DATE_KEY, value=today))
            else:
                last_run.value = today
        session.commit()
