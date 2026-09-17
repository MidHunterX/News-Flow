# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

**News Flow** — a FastAPI app that scrapes Malayalam news websites (Kerala
Kaumudi, Mangalam, Madhyamam), caches articles in SQLite, and serves a
Jinja2 review UI where articles are accepted/rejected/completed. Accepted
articles get their cover image downloaded and content saved for viewing.

- Python **3.14**, managed with **uv** (not pip/npm-style tooling)
- Pydantic models, SQLAlchemy (async) persistence, Playwright for JS-heavy pages

## Commands

```bash
uv run pytest                 # offline test suite (must pass, no network)
uv run python scripts/health_check.py [source ...]
                              # live smoke test against real websites (manual)
uv run main.py                # run the app
uv sync                       # install/update dependencies
```

There is no separate typechecker configured; run `uv run pytest` as the
verification gate.

## Layout

```
app/
  config.py          # SOURCES dict (active sources), TIMEOUT, USER_AGENT
  models.py          # NewsItem, ScrapedArticleContent, ScrapeResponse (pydantic)
  client.py          # HttpClient singleton (httpx.AsyncClient)
  utils.py           # fetch_html, download_image, clean_text, resolve_image_url
  browser.py         # fetch_rendered_html (Playwright, for JS-rendered pages)
  scrapers/
    base.py          # BaseScraper ABC: scrape(), scrape_article_page(), needs_browser
    init.py          # SCRAPERS registry + scrape_source() pipeline
    kaumudi.py / mangalam.py / madhyamam.py
  routes/scrape.py   # API + UI routes, article accept/reject/clear, settings
  db/                # SQLAlchemy models, schema, article/settings queries
templates/           # Jinja2 UI
tests/
  fixtures/          # offline HTML fixtures, one per source page
scripts/health_check.py
```

## Testing philosophy (important)

- **Tests never touch the network.** The old live-website tests were moved
  to `scripts/health_check.py`. Keep the pytest suite offline and fast.
- Scrapers are tested against **local HTML fixtures** in `tests/fixtures/`
  (`<source>_list.html`, `<source>_article.html`). When a site's markup
  changes, update the fixture to the new real markup, then fix the scraper.
- HTTP is faked with the `FakeClient`/`monkeypatch` pattern already in
  `tests/test_scrapers.py`; no source-level `respx`/`responses` dependency.
- `tests/conftest.py` resets the `HttpClient` singleton around each test —
  keep that fixture if you add HTTP-dependent tests.
- **TDD pattern:** for unimplemented behavior, commit the fixture + test and
  mark it `pytest.xfail(...)` with a reason (see madhyamam article scraping).
  When you implement the feature, remove the xfail.
- `asyncio_mode = "auto"` in pyproject — async test functions need no
  `@pytest.mark.asyncio` decoration (existing code still has some; either
  style works).

## Gotchas learned the hard way

- **`SOURCES` vs `SCRAPERS` mismatch:** a scraper can be registered in
  `SCRAPERS` while its source is commented out of `SOURCES`
  (`app/config.py`). Scrapers must use `SOURCES.get(name, fallback_url)` —
  see `MadhyamamScraper.BASE_URL` — or they crash with `KeyError` when
  tested while disabled.
- Scrapers are **async** (`await scraper.scrape(html)`), even though they
  only parse strings.
- Mangalam is a Next.js app: images come wrapped in
  `/_next/image?url=<encoded>&w=...&q=...`; `resolve_image_url()` in
  `app/utils.py` unwraps them. Its pages sometimes render only a loading
  skeleton server-side — `needs_browser` + `fetch_rendered_html` (Playwright)
  exist for this; covered by `tests/fixtures/mangalam_article_skeleton.html`.
- Relative article/image URLs must be absolutized with `urljoin` against the
  source origin; never ship a relative `url`/`image_url` out of a scraper.
- `download_image()` returns `None` on HTTP failure instead of raising;
  callers must handle both.
- The UI reads scraped article content from `public/articles/<id>.txt`
  (written by the accept endpoint) and covers from `public/covers/`.

## Conventions

- Commit messages use **gitmoji** prefixes with scopes, e.g.
  `🐛 fix(scrapers): handle multiple br tags`, `✨ feat(ui): ...`.
- Match the existing style: `from __future__ import annotations`,
  type hints everywhere, docstrings on non-obvious functions.
- Keep changes minimal — edit existing files rather than adding parallel
  helpers; new dependencies need justification.
- Do not commit `newsflow.db*`, `.venv/`, or `__pycache__/`. `public/articles/`
  is gitignored, but `public/covers/` **is** tracked intentionally (downloaded
  cover images belong in the repo) — don't add it to `.gitignore`.
