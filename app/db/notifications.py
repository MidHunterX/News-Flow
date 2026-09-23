"""Data access for the notification log shown in the UI.

Notifications are fail-soft journal entries (e.g. "Gemini call failed for
article 12: HTTP 429"): recording one must never raise into the caller, so
every public function swallows and logs DB errors. Entries are trimmed to
:data:`app.db.constants.MAX_NOTIFICATIONS` newest rows on insert.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from app.db.constants import (MAX_MESSAGE_CHARS, MAX_NOTIFICATIONS,
                              NOTIF_ERROR, NOTIF_INFO, NOTIF_WARNING,
                              now_iso)
from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Notification

logger = logging.getLogger(__name__)


def _record_sync(
    level: str,
    source: str,
    message: str,
    article_id: int | None = None,
) -> None:
    """Insert a notification row, trimming older ones; never raises."""
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 1] + "…"
    try:
        with SessionLocal() as session:
            session.add(
                Notification(
                    level=level,
                    source=source,
                    message=message,
                    article_id=article_id,
                    created_at=now_iso(),
                )
            )
            session.flush()
            # Keep only the newest MAX_NOTIFICATIONS rows.
            session.execute(
                delete(Notification).where(
                    Notification.id.not_in(
                        select(Notification.id)
                        .order_by(Notification.id.desc())
                        .limit(MAX_NOTIFICATIONS)
                    )
                )
            )
            session.commit()
    except Exception:
        logger.warning("Could not store notification", exc_info=True)


async def record_notification(
    level: str,
    source: str,
    message: str,
    article_id: int | None = None,
) -> None:
    """Store a notification for display in the UI. Never raises."""
    await run_in_thread(_record_sync, level, source, message, article_id)


def _get_sync(limit: int = 50) -> list[Notification]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Notification)
                .order_by(Notification.id.desc())
                .limit(limit)
            ).all()
        )


async def get_notifications(limit: int = 50) -> list[Notification]:
    """Newest-first notifications, at most *limit* rows."""
    return await run_in_thread(_get_sync, max(1, min(limit, MAX_NOTIFICATIONS)))


def _clear_sync() -> int:
    with SessionLocal() as session:
        deleted = session.query(Notification).delete()
        session.commit()
        return deleted


async def clear_notifications() -> int:
    """Delete all stored notifications; returns how many rows were removed."""
    return await run_in_thread(_clear_sync)


# Re-exported for callers that branch on severity without importing constants.
ERROR = NOTIF_ERROR
INFO = NOTIF_INFO
WARNING = NOTIF_WARNING
