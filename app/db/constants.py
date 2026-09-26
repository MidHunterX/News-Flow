"""Shared constants and defaults for the database layer."""

import os
from datetime import UTC, datetime

# User-overridable settings and their default values (seeded into the DB).
DEFAULT_SETTINGS = {
    # Seconds between an article being accepted and it being marked completed.
    "completion_interval": "600",  # 10 minutes
    # How the pending article list renders: "grid" (cards in columns),
    # "rows" (full-width rows with thumbnail), or "compact" (dense rows).
    "article_layout": "grid",
    # AI Publish: how many pending articles Gemini picks per prompt, and how
    # long to wait between prompts (seconds).
    "ai_publish_count": "3",
    "ai_publish_interval": "3600",  # 1 hour
    # AI Publish: how many previously accepted headings are fed to Gemini as
    # context so it avoids picking near-duplicates across sources.
    "ai_publish_history": "10",
}

# Layouts the article grid setting accepts (validated in the settings API).
ARTICLE_LAYOUTS = ("grid", "rows", "compact")

# Feature toggles. Each one can only be switched on when its backing
# environment credentials are present (checked at runtime via
# toggle_is_available); the settings API refuses the write otherwise.
#   ai_auto_categorization: Gemini picks related WP categories for each
#     accepted article right before it is published (needs GEMINI_API_KEY).
#   ai_publish: Gemini periodically picks pending articles to accept (needs
#     GEMINI_API_KEY).
#   auto_publish: completed articles are pushed to WordPress (needs
#     WORDPRESS_URL + WORDPRESS_USERNAME + WORDPRESS_APP_PASSWORD).
TOGGLE_SETTINGS = {
    "ai_auto_categorization": "gemini",
    "ai_publish": "gemini",
    "auto_publish": "wordpress",
}

# Defaults for the toggle settings ("0"/"1" stored as strings).
DEFAULT_SETTINGS = {
    **DEFAULT_SETTINGS,
    **dict.fromkeys(TOGGLE_SETTINGS, "1"),
}

# Which env-var names must be present for each toggle to be available.
TOGGLE_ENV_KEYS = {
    "ai_auto_categorization": ("GEMINI_API_KEY",),
    "ai_publish": ("GEMINI_API_KEY",),
    "auto_publish": ("WORDPRESS_URL", "WORDPRESS_USERNAME",
                     "WORDPRESS_APP_PASSWORD"),
}


def toggle_is_available(key: str) -> bool:
    """True when every env var *key* depends on is set to a non-empty value."""
    return all(os.environ.get(name, "").strip() for name in TOGGLE_ENV_KEYS[key])


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

# Setting key storing the UTC timestamp of the last successful WordPress
# terms (categories + tags) sync. WP_CATEGORIES_TTL drives re-syncing.
WP_TERMS_SYNCED_KEY = "wp_terms_synced_at"

# Setting key storing the UTC timestamp of the last AI Publish prompt. The
# next prompt goes out ai_publish_interval seconds after it.
AI_PUBLISH_LAST_RUN_KEY = "ai_publish_last_run"

# Minimum gap (seconds) between AI Publish prompts — a floor enforced on the
# user-settable interval so a 0-second setting can't hammer the API.
AI_PUBLISH_MIN_INTERVAL = 30

# How long a WordPress terms sync stays fresh before being refreshed.
WP_CATEGORIES_TTL = 24 * 60 * 60  # seconds

# Notification log levels (stored in the notifications table, shown in the UI).
NOTIF_INFO = "info"
NOTIF_WARNING = "warning"
NOTIF_ERROR = "error"

# Cap on stored notifications; the oldest rows are trimmed on insert.
MAX_NOTIFICATIONS = 100

# Stored notification messages are truncated to this length.
MAX_MESSAGE_CHARS = 500


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")
