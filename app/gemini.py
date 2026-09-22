"""Google Gemini client for auto-categorizing articles.

``suggest_categories`` sends the article (heading + body + description) along
with the site's synced WordPress categories and tags, and asks Gemini to
respond with every related category as JSON (structured output via
responseMimeType/responseSchema). The response names are matched back against
the synced categories case-insensitively, so only real WordPress category IDs
are ever returned. Fail-soft: any error logs and returns an empty list.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import GEMINI_API_KEY, GEMINI_MODEL
from app.models import NewsItem

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Hard cap on the article text sent to Gemini (keeps prompts small; the news
# body is a scrape, not literature).
MAX_BODY_CHARS = 4000

# A synced term as fed to the model/matcher: (wp_term_id, name, slug).
TermTriple = tuple[int, str, str]

_SYSTEM_PROMPT = (
    "You categorize Malayalam news articles for a WordPress news site. "
    "You are given an article (heading, description, body) and the site's "
    "available categories and tags. Respond with every category related to "
    "the article (1 to 4 of them), using the exact category names given. "
    "The tags are extra context about the site's topic vocabulary; never "
    "return them as categories. If no category fits, return an empty list. "
    "Never invent categories."
)

# Structured-output schema: responseMimeType + responseSchema force JSON.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Related category names, exactly as given",
        },
    },
    "required": ["categories"],
}


def is_configured() -> bool:
    """True when a Gemini API key is present."""
    return bool(GEMINI_API_KEY)


def _article_text(article: NewsItem, heading: str, body: str) -> str:
    """Build the article portion of the prompt."""
    parts = [f"Heading: {heading or article.title}"]
    if article.description:
        parts.append(f"Description: {article.description}")
    if body:
        parts.append(f"Body:\n{body[:MAX_BODY_CHARS]}")
    return "\n\n".join(parts)


def _terms_prompt(categories: list[TermTriple], tags: list[TermTriple]) -> str:
    """Render the available-terms portion of the prompt as name: slug lines."""
    def render(label: str, terms: list[TermTriple]) -> list[str]:
        if not terms:
            return [f"{label} (none)"]
        return [label] + [f"- {name} (slug: {slug})" for _, name, slug in terms]

    return "\n".join(
        render("Available categories:", categories)
        + [""]
        + render("Available tags (context only):", tags)
    )


def build_prompt(
    article: NewsItem,
    heading: str,
    body: str,
    categories: list[TermTriple],
    tags: list[TermTriple],
) -> str:
    """Full user prompt: article text followed by the available terms."""
    return (
        f"{_article_text(article, heading, body)}\n\n"
        f"{_terms_prompt(categories, tags)}\n\n"
        "Respond with all related categories."
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the model response, tolerating markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response is not a JSON object")
    return parsed


def _match_terms(names: Any, available: list[TermTriple]) -> list[int]:
    """Map model-returned term names/slugs back to WordPress term IDs.

    *available* holds (wp_id, name, slug) triples. Matching is
    case-insensitive against name and slug; unknown names are dropped so
    only real site terms can end up on a post.
    """
    if not isinstance(names, list):
        return []
    index: dict[str, int] = {}
    for wp_id, name, slug in available:
        index[name.strip().casefold()] = wp_id
        index[slug.casefold()] = wp_id

    matched: list[int] = []
    for name in names:
        if not isinstance(name, str):
            continue
        wp_id = index.get(name.strip().casefold())
        if wp_id is not None and wp_id not in matched:
            matched.append(wp_id)
    return matched


async def suggest_categories(
    article: NewsItem, heading: str, body: str
) -> list[int]:
    """Ask Gemini which site categories relate to *article*; return their IDs.

    Returns [] when Gemini is not configured, no categories are synced, or
    the request/parsing fails — the caller then publishes uncategorized.
    """
    if not is_configured():
        return []

    from app.client import get_client  # lazy: tests patch app.client.get_client
    from app.db.wp_terms import get_categories, get_tags

    category_triples = [
        (c.wp_id, c.name, c.slug) for c in await get_categories()
    ]
    if not category_triples:
        logger.warning("No synced WordPress categories; skipping Gemini")
        return []
    tag_triples = [(t.wp_id, t.name, t.slug) for t in await get_tags()]

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": build_prompt(
            article, heading, body, category_triples, tag_triples
        )}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }
    try:
        client = await get_client()
        resp = await client.post(
            f"{API_BASE}/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _extract_json(text)
    except Exception as exc:
        # Fail-soft like publishing: log and skip categorization.
        logger.warning("Gemini categorization failed: %s", exc)
        return []

    matched = _match_terms(parsed.get("categories"), category_triples)
    logger.info(
        "Gemini suggested categories %s for article %s",
        matched, article.id,
    )
    return matched
