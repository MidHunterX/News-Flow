"""Google Gemini client.

Two AI features share this module:

* ``suggest_categories`` sends an accepted article (heading + body +
  description) along with the site's synced WordPress categories and tags,
  and asks Gemini to respond with every related category as JSON (structured
  output via responseMimeType/responseSchema). The response names are matched
  back against the synced categories case-insensitively, so only real
  WordPress category IDs are ever returned. Fail-soft: any error logs and
  returns an empty list.
* ``pick_article_ids`` asks Gemini to pick the most newsworthy pending
  articles for publication (the AI Publish feature). It receives the pending
  articles as "<id>. <heading>" lines plus recently accepted headings as
  duplicate-avoidance context, and responds with up to *count* article IDs
  via a JSON schema. Matching drops any ID not in the offered list, so a
  hallucinated ID can never accept a real article.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config import GEMINI_API_KEY, GEMINI_MODEL
from app.db.constants import NOTIF_WARNING
from app.models import NewsItem

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Hard cap on the article text sent to Gemini (keeps prompts small; a heading
# plus a short body excerpt is enough for categorization).
MAX_BODY_CHARS = 360

# Gemini (a thinking model) regularly needs >20s, longer than the shared
# scrape client's TIMEOUT, so generateContent gets its own.
REQUEST_TIMEOUT = httpx.Timeout(60.0)

# Transient failures worth retrying with a short backoff.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3

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

# --- AI Publish (article selection) -----------------------------------------

_PICK_SYSTEM_PROMPT = (
    "You are an editor for a Malayalam WordPress news site. You are given a "
    "list of pending news articles as 'ID. heading' lines, and a list of "
    "headings that were already accepted recently. Pick exactly the requested "
    "number of the most newsworthy articles, preferring diverse topics and "
    "sources. Never pick a story that duplicates or merely paraphrases a "
    "recently accepted heading, even when it comes from a different source. "
    "Respond with JSON only: {{\"ids\": [<article IDs>]}} containing at most "
    "the requested number of IDs, chosen only from the given list."
)

# Structured-output schema for the AI Publish selection response.
_PICK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Chosen pending article IDs",
        },
    },
    "required": ["ids"],
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
        resp = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(
                    f"{API_BASE}/models/{GEMINI_MODEL}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code not in _RETRY_STATUSES:
                    break
                retry_after = float(
                    resp.headers.get("retry-after") or 0
                )
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                retry_after = 1.0
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(5.0, retry_after or 0.5 * (attempt + 1)))
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _extract_json(text)
    except Exception as exc:
        # Fail-soft like publishing: log and skip categorization. Include the
        # type name — transport timeouts stringify to an empty message.
        logger.warning("Gemini categorization failed: %s: %s",
                       type(exc).__name__, exc)
        # Surface it in the UI notification log (itself fail-soft).
        from app.db.notifications import record_notification
        await record_notification(
            NOTIF_WARNING,
            "gemini",
            f"Categorization failed ({type(exc).__name__}): {exc or 'no details'}",
            article_id=article.id,
        )
        return []

    matched = _match_terms(parsed.get("categories"), category_triples)
    logger.info(
        "Gemini suggested categories %s for article %s",
        matched, article.id,
    )
    return matched


# ---------------------------------------------------------------------------
# AI Publish: ask Gemini to pick pending articles for publication
# ---------------------------------------------------------------------------


def build_pick_prompt(
    pending: list[tuple[int, str]],
    recent_accepted: list[str],
    count: int,
) -> str:
    """User prompt for AI Publish: pending "<id>. <heading>" lines + context.

    *pending* holds (article_id, heading) pairs; *recent_accepted* holds
    headings already accepted recently (duplicate-avoidance context).
    """
    lines = [f"{article_id}. {heading}" for article_id, heading in pending]
    if recent_accepted:
        context_lines = "\n".join(f"- {heading}" for heading in recent_accepted)
        context = (
            f"\n\nRecently accepted headings (avoid picking stories that "
            f"duplicate these):\n{context_lines}"
        )
    else:
        context = "\n\nRecently accepted headings (avoid picking stories that duplicate these): (none)"
    return (
        f"Pending articles:\n{chr(10).join(lines)}\n"
        f"{context}\n\n"
        f"Pick {count} of the most newsworthy articles."
    )


def _match_ids(ids: Any, pending_ids: list[int]) -> list[int]:
    """Map model-returned IDs back onto the offered pending IDs.

    Unknown, non-integer, or duplicate entries are dropped, and the order
    follows the pending list so accepted_order lands chronologically. A
    hallucinated ID can therefore never accept a real article.
    """
    if not isinstance(ids, list):
        return []
    offered = set(pending_ids)
    seen: set[int] = set()
    matched: list[int] = []
    for raw in ids:
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        if raw in offered and raw not in seen:
            seen.add(raw)
            matched.append(raw)
    # Keep the prompt's (oldest-first) order rather than the model's ranking.
    return [pid for pid in pending_ids if pid in seen]


async def pick_article_ids(
    pending: list[tuple[int, str]], recent_accepted: list[str], count: int
) -> list[int]:
    """Ask Gemini to pick *count* pending articles; return their IDs.

    Returns [] when Gemini is not configured, nothing is pending, or the
    request/parsing fails — the caller then simply waits for the next
    interval. Fail-soft like every other Gemini call.
    """
    if not is_configured() or not pending or count < 1:
        return []

    from app.client import get_client  # lazy: tests patch app.client.get_client

    payload = {
        "systemInstruction": {"parts": [{"text": _PICK_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": build_pick_prompt(
            pending, recent_accepted, count
        )}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _PICK_RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }
    try:
        client = await get_client()
        resp = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(
                    f"{API_BASE}/models/{GEMINI_MODEL}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code not in _RETRY_STATUSES:
                    break
                retry_after = float(resp.headers.get("retry-after") or 0)
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                retry_after = 1.0
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(5.0, retry_after or 0.5 * (attempt + 1)))
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _extract_json(text)
    except Exception as exc:
        # Fail-soft: a 429 or any other error just skips this round.
        logger.warning("Gemini article selection failed: %s: %s",
                       type(exc).__name__, exc)
        from app.db.notifications import record_notification
        await record_notification(
            NOTIF_WARNING,
            "gemini",
            f"AI Publish selection failed ({type(exc).__name__}): "
            f"{exc or 'no details'}",
        )
        return []

    matched = _match_ids(parsed.get("ids"), [pid for pid, _ in pending])
    logger.info("Gemini picked articles %s for AI Publish", matched)
    return matched
