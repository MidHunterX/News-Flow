# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

**News Flow** — a FastAPI app that scrapes Malayalam news websites (Kerala
Kaumudi, Mangalam, Madhyamam), caches articles in SQLite (WAL mode), and
serves a Jinja2 review UI where articles are accepted/rejected/completed.
Accepted articles get their cover image downloaded to `public/covers/` and
their scraped content saved to `public/articles/<id>.txt`.

- Python **3.14**, managed with **uv** (not pip/npm-style tooling)
- Pydantic models, **synchronous** SQLAlchemy 2.0 persistence wrapped in
  `asyncio.to_thread` (not an async engine), BeautifulSoup4 parsing,
  httpx fetching (via `fastapi[standard]`), Playwright for JS-heavy pages
- A background asyncio task auto-completes accepted articles once
  `completion_interval` (a user-editable setting, default 600 s) elapses

## Commands

```bash
uv run fastapi dev main.py    # run the app (http://localhost:8000)
                              # NOTE: `uv run main.py` does NOT serve —
                              # main.py only defines the app object
uv run pytest                 # offline test suite (must pass, no network)
uv run python scripts/health_check.py [source ...]
                              # live smoke test against real websites (manual)
uv sync                       # install/update dependencies
uv run playwright install chromium   # once, for browser-rendered pages
```

There is no separate typechecker configured; run `uv run pytest` as the
verification gate.

## Layout

```
main.py              # FastAPI app, startup/shutdown, completion loop
app/
  config.py          # SOURCES dict (active sources), TIMEOUT, USER_AGENT,
                     # MAX_ARTICLES_PER_SOURCE
  models.py          # NewsItem, ScrapedArticleContent, ScrapeResponse (pydantic)
  client.py          # HttpClient singleton (httpx.AsyncClient)
  utils.py           # fetch_html, download_image, clean_text, resolve_image_url,
                     # PUBLIC_DIR / ARTICLES_DIR / COVERS_DIR paths
  browser.py         # fetch_rendered_html (Playwright, for JS-rendered pages)
  scrapers/
    base.py          # BaseScraper ABC: scrape(), scrape_article_page(), needs_browser
    init.py          # SCRAPERS registry + scrape_source() pipeline
                     # (the file is literally named init.py, not __init__.py)
    kaumudi.py / mangalam.py / madhyamam.py
  routes/scrape.py   # API + UI routes, article accept/reject/clear, settings
  db/                # persistence layer (SQLAlchemy 2.0 + SQLite, WAL mode)
    engine.py        # engine, SessionLocal, Base, run_in_thread, DB_PATH
                     # (override the DB location with NEWSFLOW_DB_PATH)
    models.py        # Article, Setting ORM models
    constants.py     # STATUS_* flags, DEFAULT_SETTINGS, LAST_RUN_DATE_KEY
    schema.py        # create_all + column migrations + init_db (seeds settings,
                     # resets articles on a new day)
    articles.py      # article queries/mutations
    settings.py      # settings queries/mutations
templates/           # Jinja2 UI (base/index/article.html), Tailwind via CDN
static/              # logo.svg
tests/
  fixtures/          # offline HTML fixtures, one per source page
scripts/health_check.py
```

## Runtime behavior worth knowing

- **Startup (`main.py`):** `init_db()` runs, `/covers` is mounted as static
  files, and a background task marks accepted articles `completed` once
  their interval elapses (checked every 2 s).
- **Daily reset:** `init_db()` clears the entire articles table whenever the
  app starts on a new UTC day (`LAST_RUN_DATE_KEY` setting). Don't be
  surprised by an empty DB the next morning.
- **Refresh trimming:** `/api/refresh` re-scrapes and keeps at most
  `MAX_ARTICLES_PER_SOURCE` (10) articles per source.
- **Accept flow** (`routes/scrape.py`): marks the article accepted → scrapes
  its page (via Playwright when `needs_browser`) → downloads the cover to
  `public/covers/` → saves heading+content to `public/articles/<id>.txt` and
  the web path `/covers/<name>` into `article.cover_file`. Scraping failures
  are swallowed — accept never fails because of them.

## Testing philosophy (important)

- **Tests never touch the network.** The old live-website tests were moved
  to `scripts/health_check.py`. Keep the pytest suite offline and fast.
- Scrapers are tested against **local HTML fixtures** in `tests/fixtures/`
  (`<source>_list.html`, `<source>_article.html`). When a site's markup
  changes, update the fixture to the new real markup, then fix the scraper.
- HTTP is faked with the `FakeClient`/`monkeypatch` pattern already in
  `tests/test_scrapers.py` (the `fake_http` fixture patches
  `app.utils.fetch_html`; image-download tests patch
  `app.client.get_client`). No source-level `respx`/`responses` dependency.
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
- All DB access goes through the helpers in `app/db` (blocking calls run in
  a worker thread via `run_in_thread`) — don't bypass them or swap in an
  async engine casually.
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
  is gitignored, but `public/covers/` is **not** ignored — downloaded cover
  images are meant to be committed; don't add it to `.gitignore`.
