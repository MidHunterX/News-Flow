"""Shared constants and defaults for the database layer."""

from datetime import datetime, timezone

# User-overridable settings and their default values (seeded into the DB).
DEFAULT_SETTINGS = {
    # Seconds between an article being accepted and it being marked completed.
    "completion_interval": "600",  # 10 minutes
}

# Article status flags. NULL (default) means the article is untouched.
STATUS_ACCEPTED = "accepted"
# Transient: claimed by a publisher run (post creation is in flight). Prevents
# concurrent triggers (background loop + UI reloads) from publishing twice.
STATUS_PUBLISHING = "publishing"
STATUS_REJECTED = "rejected"
STATUS_COMPLETED = "completed"

# Setting key storing the UTC date of the last app run. When the app starts on
# a new day, the articles table is cleared for a fresh workspace.
LAST_RUN_DATE_KEY = "last_run_date"


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
