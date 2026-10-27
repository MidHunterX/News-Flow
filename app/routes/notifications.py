"""Notification-log routes behind the UI's bell button.

Split out of the scrape router so the log owns its own read-state semantics:
``GET`` serves the journal plus the unread count the badge shows (info
entries excluded), ``POST /read`` clears the unread flag on every entry the
user hasn't seen yet, and ``DELETE`` empties the log.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.db import (
    clear_notifications,
    get_notifications,
    get_unread_count,
    mark_notifications_read,
)

router = APIRouter()


@router.get("/api/notifications")
async def list_notifications(limit: int = Query(50, ge=1, le=100)):
    """Return the stored notification log (newest first) as JSON.

    ``unread_count`` drives the bell badge and deliberately skips info
    rows — only unseen warnings/errors are worth flagging.
    """
    rows = await get_notifications(limit)
    return {
        "count": len(rows),
        "unread_count": await get_unread_count(),
        "items": [
            {
                "id": row.id,
                "level": row.level,
                "source": row.source,
                "message": row.message,
                "article_id": row.article_id,
                "created_at": row.created_at,
                "is_read": row.is_read,
            }
            for row in rows
        ],
    }


@router.post("/api/notifications/read")
async def mark_all_read():
    """Mark every unseen notification as read (the bell button's click)."""
    marked = await mark_notifications_read()
    return {"ok": True, "marked": marked, "unread_count": 0}


@router.delete("/api/notifications")
async def delete_notifications():
    """Clear the entire notification log."""
    deleted = await clear_notifications()
    return {"ok": True, "deleted": deleted}
