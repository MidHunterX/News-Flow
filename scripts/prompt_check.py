"""Offline dump of the exact Gemini prompts as they would be sent.

Run manually when you want to inspect what the model actually receives
(no network, no API key needed):

    uv run python scripts/prompt_check.py                # both prompts
    uv run python scripts/prompt_check.py --categorize   # categorization only
    uv run python scripts/prompt_check.py --pick         # AI Publish only

For each AI feature in app/gemini.py the script prints the request it would
build at its call site:

1. The system instruction text.
2. The full user prompt. Every input is read from the local database first,
   so the rendering matches a real run:
   * the article (and its heading/body from ``public/articles/<id>.txt``)
     comes from an accepted/completed article when one exists — the same
     content :func:`app.publisher.publish_article` feeds to
     :func:`app.gemini.suggest_categories`;
   * the synced WordPress categories come from the ``wp_terms`` table;
   * the pending headings, the recently-accepted headings and the pick count
     come from the ``articles``/``settings`` tables — the same values the
     AI Publish curator passes to :func:`app.gemini.pick_article_ids`.
   The sample article/terms/pending data below stands in only when the
   database is unreadable or a table is still empty (each fallback is
   announced with a ``(…; using sample …)`` note).
3. The response schema sent alongside the prompt in generationConfig.

The sample body is deliberately longer than MAX_BODY_CHARS so you can see
the truncation the model actually experiences whenever the real article body
is short or missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make "app" importable when running from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.gemini import (
    _PICK_RESPONSE_SCHEMA,
    _PICK_SYSTEM_PROMPT,
    _RESPONSE_SCHEMA,
    _SYSTEM_PROMPT,
    MAX_BODY_CHARS,
    TermTriple,
    build_pick_prompt,
    build_prompt,
)
from app.models import NewsItem

# Deliberately unambiguous sample: a cricket match should trip any sports
# category on a news site (same sample as scripts/publish_check.py).
SAMPLE = NewsItem(
    id=0,
    title="Kerala seal thriller against Karnataka in Ranji Trophy opener",
    url=None,
    image_url=None,
    description=(
        "Kerala chased down 287 on the final day to beat Karnataka by "
        "three wickets, with the captain scoring an unbeaten century."
    ),
    published_at="2026-09-22",
    source="prompt-preview",
)

BODY = (
    "Kerala completed a stunning chase on the final day of the Ranji "
    "Trophy opener in Thiruvananthapuram, reaching 287 for seven with seven "
    "overs to spare. The visitors had set an intimidating target after "
    " Karnataka's first-innings 350, built around a patient double "
    "century from their opener. But Kerala's reply was anchored by an "
    "unbeaten 132 from the captain, who shepherded the lower order through "
    " a tense fourth morning as wickets fell at regular intervals. "
    "The winning runs came off the bat of the No. 9, who had survived a "
    "fiery spell either side of lunch. Kerala next face Tamil Nadu at "
    "home, while Karnataka travel to Hyderabad for their second fixture."
)

# Sample terms, shown only when the DB holds no synced WordPress terms.
SAMPLE_CATEGORIES: list[TermTriple] = [
    (101, "Sports", "sports"),
    (102, "Kerala", "kerala"),
    (103, "National", "national"),
    (104, "Cricket", "cricket"),
]


SAMPLE_PENDING: list[tuple[int, str]] = [
    (401, "Kerala seal thriller against Karnataka in Ranji Trophy opener"),
    (402, "Union Cabinet approves new coastal highway for Kerala"),
    (403, "Heavy rain warning issued for Idukki and Wayanad districts"),
    (404, "Kerala Blasters sign Brazilian striker on two-year deal"),
    (405, "Gold prices hit record high ahead of festive season"),
]
SAMPLE_RECENT: list[str] = [
    "Onam markets see record flower sales as demand surges",
    "New flight connections announced for Kannur airport",
]
PICK_COUNT = 3

# Set once per run: _DB_TRIED after the first open attempt, _DB_OK when that
# attempt succeeded. A failed open sends every loader to its sample fallback
# (and is reported only once, not once per loader).
_DB_TRIED = False
_DB_OK = False


def _init_db() -> None:
    """Open/migrate the local database once; remember if that failed."""
    global _DB_TRIED, _DB_OK
    if _DB_TRIED:
        return
    _DB_TRIED = True
    try:
        from app.db import init_db

        init_db()
        _DB_OK = True
    except Exception as exc:
        print(
            f"(db unavailable: {type(exc).__name__}: {exc}; using sample data)"
        )


async def _load_synced_terms() -> list[TermTriple]:
    """Return synced categories from the DB, falling back to samples.

    Never touches the network: only reads what sync_terms() previously
    stored in the local SQLite database.
    """
    _init_db()
    if _DB_OK:
        try:
            from app.db import get_categories

            categories = [
                (c.wp_id, c.name, c.slug) for c in await get_categories()
            ]
        except Exception as exc:
            print(f"(db read failed: {type(exc).__name__}: {exc}; using sample terms)")
            return SAMPLE_CATEGORIES
        if categories:
            print(f"(using synced terms from DB: {len(categories)} categories)")
            return categories
        print("(no synced categories in DB; using sample terms)")
    return SAMPLE_CATEGORIES


async def _load_article() -> tuple[NewsItem, str, str]:
    """Return (article, heading, body) for the categorization prompt.

    Prefers a real article the publisher would actually categorize —
    accepted first, then completed, then anything cached — and reads its
    heading/body exactly as :func:`app.publisher.read_article_content` does.
    Falls back to the sample article (and sample body) when the DB is
    unreadable or empty.
    """
    _init_db()
    if _DB_OK:
        try:
            from app.db import get_accepted_items, get_completed_items, get_items
            from app.publisher import read_article_content

            # Accepted first (those are the articles actually categorized),
            # then completed, then anything cached; deduped by ID.
            candidates: list[NewsItem] = []
            for rows in (
                await get_accepted_items(),
                await get_completed_items(),
                await get_items(),
            ):
                candidates.extend(rows)
            seen: set[int] = set()
            unique: list[NewsItem] = []
            for item in candidates:
                if item.id is None or item.id in seen:
                    continue
                seen.add(item.id)
                unique.append(item)
        except Exception as exc:
            print(
                f"(db read failed: {type(exc).__name__}: {exc}; "
                "using sample article)"
            )
            return SAMPLE, SAMPLE.title, BODY
        if unique:
            # Prefer an article whose scraped content is on disk, so heading
            # and body are the real ones the publisher would send.
            article = unique[0]
            heading, body = "", ""
            for candidate in unique:
                candidate_heading, candidate_body = read_article_content(
                    candidate.id
                )
                if candidate_body:
                    article, heading, body = (
                        candidate,
                        candidate_heading,
                        candidate_body,
                    )
                    break
            if not body:
                # No saved content anywhere: keep the sample body so the
                # truncation note below stays meaningful.
                print(
                    f"(no saved content for article {article.id}; "
                    "using sample body)"
                )
                body = BODY
            print(
                f"(using article from DB: id={article.id}, "
                f"source={article.source}, {article.title!r})"
            )
            return article, heading, body
        print("(no articles in DB; using sample article)")
    return SAMPLE, SAMPLE.title, BODY


async def _load_pick_inputs() -> tuple[
    list[tuple[int, str]], list[str], int
]:
    """Return (pending, recent_accepted, count) for the AI Publish prompt.

    Reads exactly what the curator passes to ``pick_article_ids``; each
    field falls back to its sample independently when the DB is unreadable
    or that table/setting is empty.
    """
    _init_db()
    if not _DB_OK:
        return SAMPLE_PENDING, SAMPLE_RECENT, PICK_COUNT

    pending = SAMPLE_PENDING
    recent = SAMPLE_RECENT
    count = PICK_COUNT
    try:
        from app.db import (
            get_ai_publish_count,
            get_ai_publish_history,
            get_pending_items,
            get_recent_accepted_titles,
        )

        rows = await get_pending_items()
        real_pending = [
            (item.id, item.title) for item in rows if item.id is not None
        ]
        if real_pending:
            pending = real_pending
            print(f"(using {len(pending)} pending articles from DB)")
        else:
            print("(no pending articles in DB; using sample pending list)")

        history_limit = await get_ai_publish_history()
        recent_rows = await get_recent_accepted_titles(history_limit)
        titles = [title for _, title in recent_rows]
        if titles:
            recent = titles
            print(f"(using {len(recent)} recent accepted headings from DB)")
        else:
            print("(no recently accepted headings in DB; using sample list)")

        count = await get_ai_publish_count()
        print(f"(using pick count from settings: {count})")
    except Exception as exc:
        print(
            f"(db read failed: {type(exc).__name__}: {exc}; "
            "using sample pick inputs)"
        )
        return SAMPLE_PENDING, SAMPLE_RECENT, PICK_COUNT
    return pending, recent, count


def _print_prompt(title: str, system: str, user: str, schema: dict) -> None:
    """Print one full request: system instruction, user prompt, schema."""
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print("--- system instruction " + "-" * 47)
    print(system)
    print("--- user prompt " + "-" * 55)
    print(user)
    print("--- responseSchema " + "-" * 53)
    print(json.dumps(schema, indent=2))


async def show_categorize() -> None:
    """Render the categorization request exactly as the publisher would."""
    categories = await _load_synced_terms()
    article, heading, body = await _load_article()
    user = build_prompt(article, heading, body, categories)
    _print_prompt("CATEGORIZATION PROMPT (suggest_categories)",
                  _SYSTEM_PROMPT, user, _RESPONSE_SCHEMA)
    print(f"(note: body is truncated to {MAX_BODY_CHARS} chars in the prompt)")


async def show_pick() -> None:
    """Render the AI Publish selection request exactly as the curator would."""
    pending, recent, count = await _load_pick_inputs()
    user = build_pick_prompt(pending, recent, count)
    _print_prompt("AI Publish PROMPT (pick_article_ids)",
                  _PICK_SYSTEM_PROMPT, user, _PICK_RESPONSE_SCHEMA)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump the exact Gemini prompts for visual inspection "
                    "(offline, no API key needed)"
    )
    parser.add_argument("--categorize", action="store_true",
                        help="show only the categorization prompt")
    parser.add_argument("--pick", action="store_true",
                        help="show only the AI Publish prompt")
    args = parser.parse_args()

    show_all = not args.categorize and not args.pick
    if show_all or args.categorize:
        await show_categorize()
    if show_all or args.pick:
        await show_pick()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
