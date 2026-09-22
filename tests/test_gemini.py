"""Offline tests for the Gemini categorization client (app/gemini.py).

HTTP is faked with the FakeClient/FakeResponse pattern (no network).
"""

from __future__ import annotations

import httpx
import pytest

import app.gemini as gemini
from app.models import NewsItem

API_URL = (f"{gemini.API_BASE}/models/{gemini.GEMINI_MODEL}:generateContent")

CATEGORIES = [(1, "Agriculture", "agriculture"), (2, "Crime", "crime"),
              (3, "Sports", "sports"), (4, "Sports > Cricket", "sports-cricket")]
TAGS = [(10, "Kerala", "kerala"), (11, "Monsoon", "monsoon")]


class FakeResponse:
    def __init__(self, json_data=None, status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "GEMINI_MODEL", "test-model")
    # API_URL is computed at import time; keep it consistent with the patched
    # model so URL assertions hold.
    monkeypatch.setattr(gemini, "API_URL", API_URL, raising=False)
    return monkeypatch


@pytest.fixture
def synced_terms(monkeypatch, CATEGORIES=CATEGORIES, TAGS=TAGS):
    """Serve the canned category/tag lists from the wp_terms data layer."""
    async def fake_get_categories():
        return [
            type("Term", (), {"wp_id": i, "name": n, "slug": s})()
            for i, n, s in CATEGORIES
        ]

    async def fake_get_tags():
        return [
            type("Term", (), {"wp_id": i, "name": n, "slug": s})()
            for i, n, s in TAGS
        ]

    import app.db.wp_terms as wp_terms_mod

    monkeypatch.setattr(wp_terms_mod, "get_categories", fake_get_categories)
    monkeypatch.setattr(wp_terms_mod, "get_tags", fake_get_tags)
    return monkeypatch


def _article(**overrides) -> NewsItem:
    defaults = dict(
        id=1,
        title="Floods in Wayanad",
        url="https://example.com/a",
        image_url=None,
        description="Landslide hits hills",
        published_at="2026-09-19",
        source="kaumudi",
        cover_file=None,
    )
    defaults.update(overrides)
    return NewsItem(**defaults)


def _gemini_response(categories: list[str]) -> FakeResponse:
    text = '{"categories": ' + __import__("json").dumps(categories) + "}"
    return FakeResponse({
        "candidates": [{"content": {"parts": [{"text": text}]}}],
    })


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contains_article_and_terms(self):
        prompt = gemini.build_prompt(
            _article(), "Floods in Wayanad", "Heavy rain continues",
            CATEGORIES, TAGS,
        )
        assert "Floods in Wayanad" in prompt
        assert "Landslide hits hills" in prompt  # description
        assert "Heavy rain continues" in prompt  # body
        assert "- Agriculture (slug: agriculture)" in prompt
        assert "- Kerala (slug: kerala)" in prompt
        assert "context only" in prompt  # tags explicitly non-candidates

    def test_body_is_truncated(self):
        prompt = gemini.build_prompt(
            _article(), "H", "x" * (gemini.MAX_BODY_CHARS + 500),
            CATEGORIES, TAGS,
        )
        assert "x" * gemini.MAX_BODY_CHARS in prompt
        assert "x" * (gemini.MAX_BODY_CHARS + 10) not in prompt

    def test_empty_term_lists_render_placeholder(self):
        prompt = gemini.build_prompt(_article(), "H", "B", [], [])
        assert "Available categories: (none)" in prompt


# ---------------------------------------------------------------------------
# Response parsing / matching
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_plain_json(self):
        assert gemini._extract_json('{"categories": ["A"]}') == {"categories": ["A"]}

    def test_fenced_json(self):
        assert gemini._extract_json(
            '```json\n{"categories": ["A"]}\n```'
        ) == {"categories": ["A"]}

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            gemini._extract_json("no json here")


class TestMatchTerms:
    def test_matches_names_and_slugs_case_insensitively(self):
        assert gemini._match_terms(
            ["agriculture", "Sports", "SPORTS > CRICKET"], CATEGORIES
        ) == [1, 3, 4]

    def test_deduplicates(self):
        assert gemini._match_terms(["Sports", "sports", "Sports"], CATEGORIES) == [3]

    def test_unknown_names_dropped(self):
        assert gemini._match_terms(["Nonexistent"], CATEGORIES) == []

    def test_non_list_or_non_string_entries_ignored(self):
        assert gemini._match_terms(None, CATEGORIES) == []
        assert gemini._match_terms([42, None], CATEGORIES) == []


# ---------------------------------------------------------------------------
# suggest_categories end-to-end (faked HTTP)
# ---------------------------------------------------------------------------


class TestSuggestCategories:
    async def test_unconfigured_returns_empty_without_http(self, monkeypatch):
        monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
        assert await gemini.suggest_categories(_article(), "H", "B") == []

    async def test_returns_matched_ids(
        self, gemini_env, synced_terms, monkeypatch
    ):
        def handler(url, kwargs):
            assert url == f"{gemini.API_BASE}/models/test-model:generateContent"
            return _gemini_response(["Sports", "sports-cricket"])

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        ids = await gemini.suggest_categories(_article(), "H", "B")
        assert ids == [3, 4]
        # Structured output requested; API key sent via header.
        gen_config = client.calls[0][1]["json"]["generationConfig"]
        assert gen_config["responseMimeType"] == "application/json"
        assert "responseSchema" in gen_config
        assert client.calls[0][1]["headers"]["x-goog-api-key"] == "test-key"
        # The site's terms were fed to the model.
        prompt_text = client.calls[0][1]["json"]["contents"][0]["parts"][0]["text"]
        assert "Agriculture" in prompt_text and "Kerala" in prompt_text

    async def test_no_synced_categories_short_circuits(
        self, gemini_env, monkeypatch
    ):
        import app.db.wp_terms as wp_terms_mod

        async def empty():
            return []

        monkeypatch.setattr(wp_terms_mod, "get_categories", empty)
        assert await gemini.suggest_categories(_article(), "H", "B") == []

    async def test_http_failure_returns_empty(
        self, gemini_env, synced_terms, monkeypatch
    ):
        def handler(url, kwargs):
            return FakeResponse(status_code=500)

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        assert await gemini.suggest_categories(_article(), "H", "B") == []

    async def test_malformed_response_returns_empty(
        self, gemini_env, synced_terms, monkeypatch
    ):
        def handler(url, kwargs):
            # No candidates -> KeyError path -> fail-soft empty.
            return FakeResponse({})

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        assert await gemini.suggest_categories(_article(), "H", "B") == []
