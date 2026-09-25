"""Data access for app settings."""

from sqlalchemy import select

from app.db.constants import (AI_PUBLISH_LAST_RUN_KEY,
                              AI_PUBLISH_MIN_INTERVAL, ARTICLE_LAYOUTS,
                              DEFAULT_SETTINGS, TOGGLE_SETTINGS)
from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Setting


def _get_setting_sync(key: str) -> str | None:
    with SessionLocal() as session:
        setting = session.get(Setting, key)
    return setting.value if setting else None


async def get_setting(key: str, default: str) -> str:
    return await run_in_thread(_get_setting_sync, key) or default


def _set_setting_sync(key: str, value: str) -> None:
    with SessionLocal() as session:
        setting = session.get(Setting, key)
        if setting is None:
            session.add(Setting(key=key, value=value))
        else:
            setting.value = value
        session.commit()


async def set_setting(key: str, value: str) -> None:
    await run_in_thread(_set_setting_sync, key, value)


def _get_all_settings_sync() -> dict[str, str]:
    with SessionLocal() as session:
        return {
            key: value
            for key, value in session.execute(select(Setting.key, Setting.value))
        }


async def get_all_settings() -> dict[str, str]:
    return await run_in_thread(_get_all_settings_sync)


async def get_completion_interval() -> int:
    """Return the completion interval in seconds, falling back to the default."""
    raw = await get_setting(
        "completion_interval", DEFAULT_SETTINGS["completion_interval"]
    )
    try:
        return max(1, int(raw))
    except TypeError, ValueError:
        return int(DEFAULT_SETTINGS["completion_interval"])


async def get_article_layout() -> str:
    """Return the article-list layout, falling back to the default if invalid."""
    raw = await get_setting("article_layout", DEFAULT_SETTINGS["article_layout"])
    return raw if raw in ARTICLE_LAYOUTS else DEFAULT_SETTINGS["article_layout"]


async def get_ai_publish_count() -> int:
    """How many articles the AI Publish feature picks per prompt (default 3)."""
    raw = await get_setting("ai_publish_count", DEFAULT_SETTINGS["ai_publish_count"])
    try:
        return max(1, int(raw))
    except TypeError, ValueError:
        return int(DEFAULT_SETTINGS["ai_publish_count"])


async def get_ai_publish_interval() -> int:
    """Seconds to wait between AI Publish prompts, clamped to the minimum gap."""
    raw = await get_setting(
        "ai_publish_interval", DEFAULT_SETTINGS["ai_publish_interval"]
    )
    try:
        interval = int(raw)
    except TypeError, ValueError:
        interval = int(DEFAULT_SETTINGS["ai_publish_interval"])
    return max(AI_PUBLISH_MIN_INTERVAL, interval)


async def get_ai_publish_history() -> int:
    """How many previously accepted headings feed the prompt as context."""
    raw = await get_setting(
        "ai_publish_history", DEFAULT_SETTINGS["ai_publish_history"]
    )
    try:
        return max(0, int(raw))
    except TypeError, ValueError:
        return int(DEFAULT_SETTINGS["ai_publish_history"])


async def get_ai_publish_last_run() -> str | None:
    """UTC ISO timestamp of the last AI Publish prompt, or None when never run."""
    raw = await get_setting(AI_PUBLISH_LAST_RUN_KEY, "")
    return raw or None


async def get_toggle(key: str) -> bool:
    """Return a feature toggle's on/off state ("1"/"0" stored as strings).

    Unknown or invalid stored values fall back to the default (on).
    """
    default = DEFAULT_SETTINGS.get(key, "1")
    raw = await get_setting(key, default)
    if raw not in ("0", "1"):
        return default == "1"
    return raw == "1"


async def get_all_toggle_states() -> dict[str, bool]:
    """Return the on/off state of every feature toggle."""
    return {key: await get_toggle(key) for key in TOGGLE_SETTINGS}
