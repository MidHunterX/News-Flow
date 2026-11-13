"""Data access for the print-layout settings and page groupings.

Publication-specific on purpose — the three knobs that describe the
paper (how many pages it has, roughly how many words fit on one page,
and how much slack that estimate allows) belong to the InDesign tooling
on ``/publication``, not to the app-wide settings. They live here
instead of growing ``app.db.settings`` and the generic settings
endpoint, but they are still plain rows in the same ``settings`` table,
seeded by ``init_db`` like every other setting.

This module also owns the ``page_number`` placement helpers (the column
both article tables share), so everything the sidebar does to group
articles has one home.
"""

from __future__ import annotations

from sqlalchemy import select, update

from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Article, PublicationArticle, Setting

# Layout knobs, stored in the settings table and seeded on startup:
# total_pages — how big the newspaper is (the sidebar renders that many
#     pages to place articles on),
# page_words — rough word-count estimate of one full page (headings and
#     images resize, so it is a target, not a hard limit),
# word_allowance — how many words a page may still take over/under that
#     target before it counts as too tight/too loose.
PUBLICATION_LAYOUT_DEFAULTS = {
    "total_pages": "8",
    "page_words": "300",
    "word_allowance": "100",
}

# Inclusive bounds the settings endpoint enforces (same keys as the
# defaults). The allowance may not exceed the quota itself: the lower
# limit (page_words - word_allowance) has to stay a usable number.
PUBLICATION_LAYOUT_BOUNDS = {
    "total_pages": (1, 200),
    "page_words": (1, 100_000),
    "word_allowance": (0, 100_000),
}


def _validated(
    total_pages: int, page_words: int, word_allowance: int
) -> tuple[int, int, int]:
    """Coerce the three layout values to ints inside their bounds.

    Raises ValueError with a message fit for the API (the route turns it
    into a 400) when a value is not a whole number, out of range, or an
    allowance bigger than the quota itself.
    """
    try:
        total_pages, page_words, word_allowance = (
            int(total_pages),
            int(page_words),
            int(word_allowance),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "total_pages, page_words and word_allowance must be whole numbers"
        ) from exc
    for name, value in (
        ("total_pages", total_pages),
        ("page_words", page_words),
        ("word_allowance", word_allowance),
    ):
        low, high = PUBLICATION_LAYOUT_BOUNDS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    if word_allowance > page_words:
        raise ValueError(
            "word_allowance can't be bigger than page_words — the lower "
            "limit would drop below zero"
        )
    return total_pages, page_words, word_allowance


def _get_layout_sync() -> dict[str, int]:
    """The three layout settings, clamped into their bounds."""
    with SessionLocal() as session:
        stored = {
            key: value
            for key, value in session.execute(
                select(Setting.key, Setting.value).where(
                    Setting.key.in_(tuple(PUBLICATION_LAYOUT_DEFAULTS))
                )
            )
        }
    layout: dict[str, int] = {}
    for key, default in PUBLICATION_LAYOUT_DEFAULTS.items():
        try:
            value = int(stored.get(key) or default)
        except (TypeError, ValueError):
            value = int(default)
        low, high = PUBLICATION_LAYOUT_BOUNDS[key]
        layout[key] = min(high, max(low, value))
    # A hand-edited row can't push the lower limit below zero.
    layout["word_allowance"] = min(layout["word_allowance"], layout["page_words"])
    return layout


async def get_publication_layout() -> dict[str, int]:
    """Return total_pages / page_words / word_allowance as ints."""
    return await run_in_thread(_get_layout_sync)


def _set_layout_sync(
    total_pages: int, page_words: int, word_allowance: int
) -> dict[str, int]:
    """Persist the layout settings; clear placements on vanished pages.

    Lowering total_pages orphans every article grouped onto a page that
    no longer exists — those are unplaced in the same transaction, so
    the articles simply return to the unassigned pool instead of
    disappearing from both the sidebar and the picker.
    """
    values = dict(
        zip(
            PUBLICATION_LAYOUT_DEFAULTS,
            _validated(total_pages, page_words, word_allowance),
            strict=True,
        )
    )
    with SessionLocal() as session:
        for key, value in values.items():
            setting = session.get(Setting, key)
            if setting is None:
                session.add(Setting(key=key, value=str(value)))
            else:
                setting.value = str(value)
        for model in (Article, PublicationArticle):
            session.execute(
                update(model)
                .where(model.page_number > values["total_pages"])
                .values(page_number=None)
            )
        session.commit()
    return values


async def set_publication_layout(
    total_pages: int, page_words: int, word_allowance: int
) -> dict[str, int]:
    """Save the layout settings (raises ValueError on invalid input)."""
    return await run_in_thread(
        _set_layout_sync, total_pages, page_words, word_allowance
    )


def _assign_sync(
    article_ids: list[int], page: int, archived: bool
) -> list[int]:
    """Place *article_ids* on *page*; return the ids that don't exist.

    All-or-nothing: a single unknown id leaves every placement untouched,
    so a stale picker list can't half-apply. Re-placing an article that
    already sits on a page simply moves it. Raises ValueError when the
    page number itself is outside the configured layout.
    """
    layout = _get_layout_sync()
    if not 1 <= page <= layout["total_pages"]:
        raise ValueError(
            f"page must be between 1 and {layout['total_pages']} "
            "(total_pages in the layout settings)"
        )
    ids = list(dict.fromkeys(article_ids))
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        # The workspace holds at most a few hundred rows, so one small
        # query beats an IN() list that could outgrow SQLite's bound
        # parameters when a big selection is placed at once.
        found = set(session.scalars(select(model.id)))
        missing = [article_id for article_id in ids if article_id not in found]
        if missing:
            return missing  # nothing written: the batch is all-or-nothing
        for article_id in ids:
            session.get(model, article_id).page_number = page
        session.commit()
    return []


async def assign_articles_to_page(
    article_ids: list[int], page: int, *, archived: bool = False
) -> list[int]:
    """Group articles onto a page. Returns the ids not found (none = ok)."""
    return await run_in_thread(_assign_sync, article_ids, page, archived)


def _clear_page_sync(article_id: int, archived: bool) -> bool:
    """Take an article off its page. False when the row doesn't exist."""
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        article = session.get(model, article_id)
        if article is None:
            return False
        article.page_number = None
        session.commit()
    return True


async def clear_article_page(article_id: int, *, archived: bool = False) -> bool:
    """Remove an article from its page, back into the unassigned pool."""
    return await run_in_thread(_clear_page_sync, article_id, archived)
