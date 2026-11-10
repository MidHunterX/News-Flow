"""Last observed scrape health of each news source.

A source being unreachable (a 404 while the site is down for maintenance, a
timeout, a parse crash) must not take the whole app with it: the scrape
pipeline isolates per-source failures, and this module remembers the outcome
so the curation UI can flag the source as down while the others keep flowing.

State is deliberately in-memory. The background refresher retries every
source on its ``refresh_interval`` (and immediately at startup), so a restart
simply rediscovers which sources are unavailable — there is nothing here
worth persisting.
"""

from __future__ import annotations

from datetime import UTC, datetime

STATUS_OK = "ok"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"

# Keep error strings short enough to sit in a tooltip / banner line.
MAX_DETAIL_CHARS = 200

_UNKNOWN: dict[str, str | None] = {
    "state": STATUS_UNKNOWN,
    "detail": None,
    "checked_at": None,
}

_status: dict[str, dict[str, str | None]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_success(name: str) -> None:
    """Remember that *name* scraped cleanly on the last attempt."""
    _status[name] = {
        "state": STATUS_OK,
        "detail": None,
        "checked_at": _now_iso(),
    }


def record_failure(name: str, exc: BaseException) -> None:
    """Remember that *name* could not be scraped, and why."""
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 1] + "…"
    _status[name] = {
        "state": STATUS_DOWN,
        "detail": detail,
        "checked_at": _now_iso(),
    }


def get_source_status(name: str) -> dict[str, str | None]:
    """Last observed status for one source (``unknown`` if never scraped)."""
    return dict(_status.get(name, _UNKNOWN))


def get_source_statuses(names: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """Status of every named source, in the given order."""
    return {name: get_source_status(name) for name in names}


def is_down(name: str) -> bool:
    """True when the last scrape attempt for *name* failed."""
    return _status.get(name, _UNKNOWN)["state"] == STATUS_DOWN


def reset() -> None:
    """Forget every observation (used by tests)."""
    _status.clear()
