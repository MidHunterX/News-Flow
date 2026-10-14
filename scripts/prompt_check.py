"""Offline dump of the exact Gemini prompts as they would be sent.

Run manually when you want to inspect what the model actually receives
(no network, no API key needed):

    uv run python scripts/prompt_check.py                # both prompts
    uv run python scripts/prompt_check.py --categorize   # categorization only
    uv run python scripts/prompt_check.py --pick         # AI Publish only

For each AI feature in app/gemini.py the script prints the request it would
build at its call site:

1. The system instruction text.
2. The full user prompt rendered from a sample article (categorization) or
   a sample pending/recent-heading input (AI Publish). For the
   categorization prompt the synced WordPress terms are read from the DB
   when available, so the rendering matches a real run; the sample terms
   below stand in when the DB is empty or WordPress is not configured.
3. The response schema sent alongside the prompt in generationConfig.

The sample body is deliberately longer than MAX_BODY_CHARS so you can see
the truncation the model actually experiences.
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
SAMPLE_TAGS: list[TermTriple] = [
    (201, "Ranji Trophy", "ranji-trophy"),
    (202, "Kerala cricket", "kerala-cricket"),
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


async def _load_synced_terms() -> tuple[list[TermTriple], list[TermTriple]]:
    """Return synced (categories, tags) from the DB, falling back to samples.

    Never touches the network: only reads what sync_terms() previously
    stored in the local SQLite database.
    """
    try:
        from app.db import get_categories, get_tags, init_db

        init_db()
        categories = [
            (c.wp_id, c.name, c.slug) for c in await get_categories()
        ]
        tags = [(t.wp_id, t.name, t.slug) for t in await get_tags()]
    except Exception as exc:
        print(f"(db read failed: {type(exc).__name__}: {exc}; using sample terms)")
        return SAMPLE_CATEGORIES, SAMPLE_TAGS
    if not categories:
        print("(no synced categories in DB; using sample terms)")
        return SAMPLE_CATEGORIES, SAMPLE_TAGS
    print(
        f"(using synced terms from DB: {len(categories)} categories, "
        f"{len(tags)} tags)"
    )
    return categories, tags


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
    categories, tags = await _load_synced_terms()
    user = build_prompt(SAMPLE, SAMPLE.title, BODY, categories, tags)
    _print_prompt("CATEGORIZATION PROMPT (suggest_categories)",
                  _SYSTEM_PROMPT, user, _RESPONSE_SCHEMA)
    print(f"(note: body is truncated to {MAX_BODY_CHARS} chars in the prompt)")


def show_pick() -> None:
    """Render the AI Publish selection request exactly as the curator would."""
    user = build_pick_prompt(SAMPLE_PENDING, SAMPLE_RECENT, PICK_COUNT)
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
        show_pick()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
