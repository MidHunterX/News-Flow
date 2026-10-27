"""Offline tests for the AI Publish curator (app/curator.py).

The Gemini call and DB layer are faked/monkeypatched — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.db as db_pkg
from app import curator
from app.db.models import Article, PublicationArticle
from app.models import NewsItem, ScrapedArticleContent

# Captured at import time: the module's autouse `_patch_pick` fixture pulls in
# `env`, which swaps enrich_accepted_article for a fake in every test. The
# workspace-targeting tests below need the real function.
_REAL_ENRICH = curator.enrich_accepted_article


def _item(idx: int, **overrides) -> NewsItem:
    defaults = {
        "id": idx,
        "title": f"Article {idx}",
        "url": f"https://example.com/{idx}",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-09-19",
        "source": "kaumudi",
    }
    defaults.update(overrides)
    return NewsItem(**defaults)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class _Settings:
    """Mutable stand-in for the settings the curator reads."""

    def __init__(self, toggle: bool = True, last_run: str | None = None,
                 interval: int = 3600, count: int = 3, history: int = 10,
                 pending_limit: int = 20):
        self.toggle = toggle
        self.last_run = last_run
        self.interval = interval
        self.count = count
        self.history = history
        self.pending_limit = pending_limit


@pytest.fixture
def env(monkeypatch):
    """Patch every DB/settings dependency the curator touches."""
    settings = _Settings()
    notifications: list[tuple[str, str, str]] = []
    accepted_ids: list[int] = []
    cleared: list[int] = []
    enriched: list[int] = []

    async def fake_get_toggle(key: str) -> bool:
        assert key == "ai_publish"
        return settings.toggle

    async def fake_get_interval() -> int:
        return settings.interval

    async def fake_get_last_run() -> str | None:
        return settings.last_run

    async def fake_get_count() -> int:
        return settings.count

    async def fake_get_history() -> int:
        return settings.history

    async def fake_get_pending_limit() -> int:
        return settings.pending_limit

    async def fake_get_pending_items() -> list[NewsItem]:
        return list(settings.pending)

    async def fake_get_recent_accepted_titles(limit: int):
        return [("src", f"Recent {i}") for i in range(min(limit, 2))]

    async def fake_set_setting(key: str, value: str) -> None:
        if key == "ai_publish_last_run":
            settings.last_run = value

    async def fake_record_notification(level, source, message, article_id=None):
        notifications.append((level, source, message))

    async def fake_set_article_status(article_id: int, status) -> bool:
        if status == "accepted":
            accepted_ids.append(article_id)
        elif status is None:
            cleared.append(article_id)
        return True

    async def fake_enrich(article: NewsItem) -> bool:
        enriched.append(article.id)
        return True  # scraping succeeded by default

    monkeypatch.setattr("app.db.get_toggle", fake_get_toggle)
    # The rest are bound into app.curator's namespace at import time.
    monkeypatch.setattr(curator, "get_ai_publish_interval", fake_get_interval)
    monkeypatch.setattr(curator, "get_ai_publish_last_run", fake_get_last_run)
    monkeypatch.setattr(curator, "get_ai_publish_count", fake_get_count)
    monkeypatch.setattr(curator, "get_ai_publish_history", fake_get_history)
    monkeypatch.setattr(
        curator, "get_ai_publish_pending_limit", fake_get_pending_limit
    )
    monkeypatch.setattr(curator, "get_pending_items", fake_get_pending_items)
    monkeypatch.setattr(
        curator, "get_recent_accepted_titles", fake_get_recent_accepted_titles
    )
    monkeypatch.setattr(curator, "set_setting", fake_set_setting)
    monkeypatch.setattr(curator, "record_notification", fake_record_notification)
    monkeypatch.setattr(curator, "set_article_status", fake_set_article_status)
    monkeypatch.setattr(curator, "enrich_accepted_article", fake_enrich)

    settings.pending = [_item(1), _item(2), _item(3)]
    settings.notifications = notifications
    settings.accepted_ids = accepted_ids
    settings.cleared = cleared
    settings.enriched = enriched
    settings.picked = [2, 3]
    return settings


@pytest.fixture(autouse=True)
def _patch_pick(monkeypatch, env):
    """Fake Gemini's pick (lazy import inside run_due_selection)."""

    async def fake_pick(pending, recent, count):
        env.pick_calls = (pending, recent, count)
        return env.picked

    monkeypatch.setattr("app.gemini.pick_article_ids", fake_pick)


async def test_disabled_toggle_is_noop(env):
    env.toggle = False
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []
    assert env.last_run is None


async def test_interval_not_elapsed_is_noop(env):
    env.last_run = _iso(datetime.now(UTC) - timedelta(seconds=60))
    env.interval = 3600
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_runs_when_interval_elapsed(env):
    env.last_run = _iso(datetime.now(UTC) - timedelta(seconds=3601))
    env.interval = 3600
    assert await curator.run_due_selection() == 2
    assert env.accepted_ids == [2, 3]


async def test_runs_when_never_run(env):
    env.last_run = None
    assert await curator.run_due_selection() == 2
    assert env.accepted_ids == [2, 3]


async def test_last_run_stamped_before_call(env, monkeypatch):
    """The attempt timestamp is stored before the Gemini call."""
    stamps: list[str] = []

    real_set = curator.set_setting

    async def tracking_set(key, value):
        stamps.append("set" if key == "ai_publish_last_run" else key)
        await real_set(key, value)

    async def slow_pick(pending, recent, count):
        stamps.append("pick")
        return []

    monkeypatch.setattr("app.gemini.pick_article_ids", slow_pick)
    monkeypatch.setattr(curator, "set_setting", tracking_set)
    await curator.run_due_selection()
    assert stamps == ["set", "pick"]


async def test_passes_id_title_pairs_and_history_to_gemini(env):
    env.history = 5
    await curator.run_due_selection()
    pending, recent, count = env.pick_calls
    assert pending == [(1, "Article 1"), (2, "Article 2"), (3, "Article 3")]
    assert recent == ["Recent 0", "Recent 1"]
    assert count == 3


async def test_no_pending_articles_skips_gemini(env, monkeypatch):
    env.pending = []
    await curator.run_due_selection()
    # pick_article_ids would still be called; it returns [] for empty input.
    assert env.accepted_ids == []


async def test_recency_limit_offers_only_the_newest_pending(env):
    """Only ai_publish_pending_limit newest articles reach the prompt."""
    env.pending = [_item(i) for i in range(1, 9)]  # newest first, as stored
    env.pending_limit = 3
    env.picked = [1]
    await curator.run_due_selection()
    pending, _recent, _count = env.pick_calls
    assert pending == [(1, "Article 1"), (2, "Article 2"), (3, "Article 3")]
    assert env.accepted_ids == [1]


async def test_zero_recency_limit_offers_every_pending_article(env):
    """0 disables the cap: the whole backlog is offered."""
    env.pending = [_item(i) for i in range(1, 9)]
    env.pending_limit = 0
    await curator.run_due_selection()
    pending, _recent, _count = env.pick_calls
    assert len(pending) == 8


async def test_recency_limit_below_the_pick_count_is_harmless(env):
    """A tight cap still works: fewer offered than Gemini may pick."""
    env.pending = [_item(7), _item(8)]
    env.pending_limit = 2
    env.picked = [7, 8]
    assert await curator.run_due_selection() == 2
    assert env.accepted_ids == [7, 8]


async def test_no_pick_means_no_accept(env, monkeypatch):
    async def empty_pick(pending, recent, count):
        return []

    monkeypatch.setattr("app.gemini.pick_article_ids", empty_pick)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_hallucinated_ids_never_accept(env, monkeypatch):
    async def bad_pick(pending, recent, count):
        return [99]  # not in the pending list

    monkeypatch.setattr("app.gemini.pick_article_ids", bad_pick)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_accept_enriches_article_for_publishing(env):
    await curator.run_due_selection()
    assert env.enriched == [2, 3]


async def test_scrape_failure_clears_status_and_counts_nothing(env, monkeypatch):
    """An article whose page can't be scraped goes back to pending."""

    async def failing_enrich(article):
        return False  # scraping failed, nothing usable was saved

    monkeypatch.setattr(curator, "enrich_accepted_article", failing_enrich)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == [2, 3]  # acceptance was attempted...
    assert env.cleared == [2, 3]       # ...but rolled back to pending
    levels = [level for level, _, _ in env.notifications]
    assert "warning" in levels


async def test_is_selecting_tracks_a_live_round(env, monkeypatch):
    """GET /api/state's "running" flag is true only while the round works."""
    seen: list[bool] = []

    async def spy_pick(pending, recent, count):
        seen.append(curator.is_selecting())
        return [2, 3]

    monkeypatch.setattr("app.gemini.pick_article_ids", spy_pick)

    assert curator.is_selecting() is False
    assert await curator.run_due_selection() == 2
    assert seen == [True]  # Gemini was called with the flag up
    assert curator.is_selecting() is False


async def test_is_selecting_clears_when_the_round_fails(env, monkeypatch):
    """A crashed round must not leave the UI stuck on "Curating…"."""

    async def boom(pending, recent, count):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.gemini.pick_article_ids", boom)

    assert await curator.run_due_selection() == 0
    assert curator.is_selecting() is False


async def test_unconfigured_gemini_skips_round(env, monkeypatch):
    monkeypatch.setattr("app.gemini.is_configured", lambda: False)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_gemini_error_is_swallowed_and_notified(env, monkeypatch):
    async def boom(pending, recent, count):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.gemini.pick_article_ids", boom)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []
    levels = [level for level, _, _ in env.notifications]
    assert "error" in levels


async def test_success_records_info_notification(env):
    await curator.run_due_selection()
    infos = [msg for level, _, msg in env.notifications if level == "info"]
    assert any("2" in msg and "AI Publish" in msg for msg in infos)


async def test_invalid_last_run_treated_as_due(env):
    env.last_run = "not-a-timestamp"
    assert await curator.run_due_selection() == 2


async def test_interval_elapsed_helper():
    assert curator._interval_elapsed(None, 60) is True


# ---------------------------------------------------------------------------
# enrich_accepted_article: which workspace it scrapes into (the publication
# page's Scrape button uses archived=True and never accepts anything).
# ---------------------------------------------------------------------------


class _FakeScraper:
    """Scraper stand-in: ignores the HTML, always returns a parsed article."""

    needs_browser = False

    async def scrape_article_page(self, html):
        return ScrapedArticleContent(
            heading="Scraped heading",
            content="one two three four",
            cover_path="https://cdn.example.com/cover.jpg",
        )


async def _fake_fetch_html(url: str) -> str:
    return "<html><body>ignored by _FakeScraper</body></html>"


class _RecordingDownload:
    """download_image stand-in: records the target dir, fakes the download."""

    def __init__(self) -> None:
        self.dest_dirs: list[Path] = []

    async def __call__(self, url, filename=None, dest_dir=None):
        self.dest_dirs.append(dest_dir)
        return str(dest_dir / Path(url).name)


def _fake_scrape_env(monkeypatch, tmp_path):
    """Redirect the scrape pipeline's I/O: workspace dirs → tmp, no network."""
    import app.utils as utils_mod
    from app.scrapers.init import SCRAPERS

    dirs = {
        "cur_articles": tmp_path / "articles",
        "cur_covers": tmp_path / "covers",
        "pub_articles": tmp_path / "publication" / "articles",
        "pub_covers": tmp_path / "publication" / "covers",
    }
    monkeypatch.setattr(utils_mod, "ARTICLES_DIR", dirs["cur_articles"])
    monkeypatch.setattr(utils_mod, "COVERS_DIR", dirs["cur_covers"])
    monkeypatch.setattr(utils_mod, "PUBLICATION_ARTICLES_DIR", dirs["pub_articles"])
    monkeypatch.setattr(utils_mod, "PUBLICATION_COVERS_DIR", dirs["pub_covers"])
    monkeypatch.setitem(SCRAPERS, "kaumudi", _FakeScraper())
    monkeypatch.setattr(utils_mod, "fetch_html", _fake_fetch_html)
    download = _RecordingDownload()
    monkeypatch.setattr(utils_mod, "download_image", download)
    # Undo `env`'s fake so the real function runs (see _REAL_ENRICH).
    monkeypatch.setattr(curator, "enrich_accepted_article", _REAL_ENRICH)
    return dirs, download


def _row(model, **overrides):
    """An article row for the table under test (its columns are shared)."""
    fields = {
        "title": "Story",
        "url": "https://example.com/1",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-10-26",
        "source": "kaumudi",
    }
    fields.update(overrides)
    return model(**fields)


async def test_default_enrich_scrapes_into_the_curation_workspace(
    tmp_path, monkeypatch
):
    """The accept flow (and the publication page's Today tab) uses curation."""
    dirs, download = _fake_scrape_env(monkeypatch, tmp_path)
    with db_pkg.SessionLocal() as session:
        session.add(_row(Article))
        session.commit()
        article_id = session.query(Article).one().id

    assert await curator.enrich_accepted_article(_item(article_id)) is True

    body_file = dirs["cur_articles"] / f"{article_id}.txt"
    assert "Scraped heading" in body_file.read_text(encoding="utf-8")
    assert not (dirs["pub_articles"] / f"{article_id}.txt").exists()
    assert download.dest_dirs == [dirs["cur_covers"]]
    with db_pkg.SessionLocal() as session:
        stored = session.get(Article, article_id)
        assert stored.cover_file == "/covers/cover.jpg"
        assert stored.status is None  # scraping never accepts an article


async def test_archived_enrich_scrapes_into_the_publication_workspace(
    tmp_path, monkeypatch
):
    """archived=True (the publication page's Scrape button) uses the mirror."""
    dirs, download = _fake_scrape_env(monkeypatch, tmp_path)
    with db_pkg.SessionLocal() as session:
        session.add(_row(PublicationArticle, status="completed"))
        session.commit()
        article_id = session.query(PublicationArticle).one().id

    assert (
        await curator.enrich_accepted_article(_item(article_id), archived=True)
    ) is True

    body_file = dirs["pub_articles"] / f"{article_id}.txt"
    assert "one two three four" in body_file.read_text(encoding="utf-8")
    assert not (dirs["cur_articles"] / f"{article_id}.txt").exists()
    assert download.dest_dirs == [dirs["pub_covers"]]
    with db_pkg.SessionLocal() as session:
        stored = session.get(PublicationArticle, article_id)
        assert stored.cover_file == "/publication/covers/cover.jpg"
        assert stored.status == "completed"  # scraping never clears status
    future = _iso(datetime.now(UTC) + timedelta(seconds=120))
    assert curator._interval_elapsed(future, 60) is False
    past = _iso(datetime.now(UTC) - timedelta(seconds=61))
    assert curator._interval_elapsed(past, 60) is True
