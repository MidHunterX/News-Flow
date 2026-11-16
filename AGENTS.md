# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

**News Flow** — a FastAPI app that scrapes Malayalam news websites (Kerala
Kaumudi, Mangalam, Madhyamam), caches articles in SQLite (WAL mode), and
serves a Jinja2 review UI where articles are accepted/rejected/completed.
Accepted articles get their cover image downloaded to `public/<db>/covers/`
and their scraped content saved to `public/<db>/articles/<id>.txt`, where
`<db>` is the active database's name (stem of `NEWSFLOW_DB_PATH`, `newsflow`
for the default `newsflow.db`). Every workspace dir — curation, covers and
the publication mirror — is scoped that way, so pointing the app at a
temporary database gives it its own tree: its startup daily reset can only
ever clear that database's (usually empty) dirs, never the main one's.

- Python **3.14**, managed with **uv** (not pip/npm-style tooling)
- Pydantic models, **synchronous** SQLAlchemy 2.0 persistence wrapped in
  `asyncio.to_thread` (not an async engine), BeautifulSoup4 parsing,
  httpx fetching (via `fastapi[standard]`), Playwright for JS-heavy pages
- A background asyncio task auto-completes accepted articles once
  `completion_interval` (a user-editable setting, default 600 s) elapses
- The same background task re-scrapes every source once `refresh_interval`
  (a user-editable setting, default 900 s) elapses (`app/refresher.py`);
  the browser never triggers refreshes
- The same background task auto-scrapes article pages while the `auto_scrape`
  toggle is on (default **off**): the oldest article with no
  `public/<db>/articles/<id>.txt` is scraped every `auto_scrape_interval` seconds
  (default 60 s) (`app/autoscraper.py`), status untouched
- WordPress publishing: categories/tags are synced from the site at startup
  (`app/wordpress.py`, TTL-refreshed) and Gemini picks related categories for
  each article (`app/gemini.py`) right before it is published

## Commands

```bash
uv run fastapi dev main.py    # run the app (http://localhost:8000)
                              # NOTE: `uv run main.py` does NOT serve —
                              # main.py only defines the app object
uv run pytest                 # offline test suite (must pass, no network)
uv run python scripts/health_check.py [source ...]
                              # live smoke test against real websites (manual)
uv run python scripts/publish_check.py [--publish] [--model]
                              # live WP terms sync + Gemini categorization check
                              # (--publish also POSTs a draft, then deletes it;
                              # --model only pings the configured Gemini model)
uv run python scripts/prompt_check.py [--categorize] [--pick]
                              # dump the exact Gemini prompts for visual
                              # inspection (offline; uses synced terms from
                              # the DB, or sample terms when it is empty)
uv sync                       # install/update dependencies
uv run ruff check .           # lint (auto-fix with --fix); runs offline
uv run playwright install chromium   # once, for browser-rendered pages
```

There is no separate typechecker configured; run `uv run pytest` as the
verification gate. `uv run ruff check .` is the lint gate (ruff is a dev
dependency, rules configured in `pyproject.toml` under `[tool.ruff.lint]`) —
run it before committing and prefer `ruff check --fix` over manual cleanup
for findings it can autofix.

## Layout

```
main.py              # FastAPI app, startup/shutdown, completion loop
app/
  config.py          # SOURCES dict (active sources), TIMEOUT, USER_AGENT,
                     # MAX_ARTICLES_PER_SOURCE, database_path() (the active
                     # SQLite file: NEWSFLOW_DB_PATH or <root>/newsflow.db)
  models.py          # NewsItem, ScrapedArticleContent, ScrapeResponse (pydantic)
  client.py          # HttpClient singleton (httpx.AsyncClient)
  utils.py           # fetch_html, download_image, clean_text, resolve_image_url,
                     # DB-scoped workspace paths: PUBLIC_DIR / DB_NAME /
                     # ARTICLES_DIR / COVERS_DIR (public/<db>/…) plus the
                     # PUBLICATION_* mirror paths
  browser.py         # fetch_rendered_html (Playwright, for JS-rendered pages)
  scrapers/
    base.py          # BaseScraper ABC: scrape(), scrape_article_page(), needs_browser
    init.py          # SCRAPERS registry + scrape_source() pipeline
                     # (the file is literally named init.py, not __init__.py)
    kaumudi.py / mangalam.py / madhyamam.py
  routes/scrape.py   # API + UI routes, article accept/reject/clear, settings
  routes/publication.py
                     # GET /publication (print-publication inspection page)
                     # + the print-layout API the sidebar drives:
                     # GET/POST /api/publication/settings, POST
                     # /api/publication/assign, POST
                     # /api/publication/{id}/unassign, GET
                     # /api/publication/export (layout JSON for the
                     # InDesign ExtendScript)
  routes/notifications.py
                     # notification log routes: GET /api/notifications (list
                     # + unread_count, info excluded), POST .../read (marks
                     # every unseen row read), DELETE ...
  routes/plugins.py  # read-only browser for the plugins/ dir: GET /plugins
                     # (folder listing), /plugins/view (text preview),
                     # /plugins/download (file, or a folder as a zip) — every
                     # path is resolved inside PLUGINS_DIR first
  db/                # persistence layer (SQLAlchemy 2.0 + SQLite, WAL mode)
    engine.py        # engine, SessionLocal, Base, run_in_thread, DB_PATH
                     # (resolved by app.config.database_path() — override
                     # the DB location with NEWSFLOW_DB_PATH)
    models.py        # Article, PublicationArticle, WpCategory, Setting ORM models
                     # (Article + PublicationArticle share the _ArticleFields
                     # mixin — the publication table mirrors the curation one)
    constants.py     # STATUS_* flags, DEFAULT_SETTINGS, LAST_RUN_DATE_KEY,
                     # WP_TERMS_SYNCED_KEY, WP_CATEGORIES_TTL
    schema.py        # create_all + column migrations + init_db (seeds settings,
                     # archives + resets articles on a new day, requeues stale
                     # claims)
    articles.py      # article queries/mutations (curation + publication)
    wp_terms.py      # WordPress terms (categories/tags) queries/mutations
    settings.py      # settings queries/mutations
    layout.py        # publication-only layout settings (total_pages,
                     # page_words, word_allowance — kept out of
                     # settings.py + /api/settings on purpose) and the
                     # page_number placement helpers both article tables
                     # share
  wordpress.py       # WordPress terms sync (categories + tags → wp_terms)
  refresher.py       # backend source refresh (refresh_sources, run_due_refresh)
  autoscraper.py     # background article scraping (run_due_auto_scrape)
  gemini.py          # Gemini categorization (article + terms → category IDs)
  source_health.py   # per-source scrape health (ok/down) surfaced in the UI;
                     # a down source is skipped, never fatal
templates/           # Jinja2 UI (base/index/publication/article/plugins.html),
                     # Tailwind via CDN
static/              # logo.svg
tests/
  fixtures/          # offline HTML fixtures, one per source page
scripts/health_check.py
```

Two workspaces exist side by side:

- **Curation workspace** — `articles` table + `public/<db>/articles/` +
  `public/<db>/covers/`; the clean-slate queue the review UI works from.
- **Publication workspace** — `publication_articles` table +
  `public/<db>/publication/{articles,covers}/`; a verbatim mirror of the
  previous day's curation workspace, filled by the daily reset and read by
  `GET /publication`. The page follows the curation UI: **Previous day**
  and **Today** pill tabs (previous day first and the default, `?day=today`
  selects the second), **ID / Word count** sort pills (`?sort=id` default,
  `?sort=word` ranks by scraped word count with unscraped articles last),
  the same results header, and the same
  `article_layout` grid/rows/compact branches. Each card carries its
  local-scrape status and word count plus two actions: **Preview** opens
  `GET /article/<id>?day=<previous|today>` (the same `article.html`, with
  its back link pointing at the tab it came from) — only for articles that
  have a local copy, since there is nothing to open otherwise — and
  **Scrape** POSTs `/api/articles/<id>/scrape?day=...`, which saves the
  page into the selected workspace *without* changing the article's status.
  **Mark published** is a toggle button (the card's print state lives on
  the button itself, not a badge): it POSTs
  `/api/publication/<id>/publish?day=...&published=1|0` to set or clear
  the row's `is_print_published` flag — the *print* meaning of
  "published", distinct from the WordPress pipeline where
  `status == "completed"` means published (the `#wp` badge).

## Runtime behavior worth knowing

- **Startup (`main.py`):** `init_db()` runs, `/covers` and
  `/publication/covers` are mounted as static files, and a background task
  marks accepted articles `completed` once their interval elapses (checked
  every 2 s).
- **Daily reset:** on a new day (per the `reset_timezone` setting,
  `LAST_RUN_DATE_KEY`) `init_db()` first clears the publication workspace,
  mirrors the curation workspace into it (rows → `publication_articles`,
  files → `public/<db>/publication/`), and only then wipes the curation
  workspace. Curation still starts empty each day; the previous day lives
  on for `/publication`. The archive holds exactly one day — each reset
  replaces it, and the AI layout generation history (`layout_generations`)
  is dropped with it: a new day starts with no saved layouts. Every dir
  involved belongs to the active database only, so a
  freshly provisioned DB (no `last_run_date` yet — e.g. a temporary
  `NEWSFLOW_DB_PATH` set up for testing) resets its own empty tree instead
  of wiping the main database's scraped content.
- **Refresh trimming:** `/api/refresh` and the scheduled background refresh
  both keep at most `MAX_ARTICLES_PER_SOURCE` (10) articles per source.
- **Down sources:** sources are scraped independently. `scrape_source()`
  records the outcome in `app/source_health.py`, and a failing source (404
  during maintenance, timeout, parse error) is skipped by `GET /`,
  `GET /api/scrape`, and both refresh paths — it never fails the page or
  costs the other sources their stories. `templates/index.html` marks it
  **Down** in the source filter (plus a banner naming the reason) and shows
  an explanation when it is the selected filter. Health is in-memory; the
  next refresh rediscovers it, and recovery is automatic.
- **Accept flow** (`routes/scrape.py`): marks the article accepted → scrapes
  its page (via Playwright when `needs_browser`) → downloads the cover to
  `public/<db>/covers/` → saves heading+content to
  `public/<db>/articles/<id>.txt` and
  the web path `/covers/<name>` into `article.cover_file`. Scraping failures
  are swallowed — accept never fails because of them. The publication
  page's **Scrape** button runs the very same `enrich_accepted_article()`
  with `archived=True` for the previous-day tab (cover →
  `public/<db>/publication/covers/`, content →
  `public/<db>/publication/articles/<id>.txt`, cover path → the
  `publication_articles` row) but never touches the article's status.
- **Print-layout sidebar** (`/publication`): the right-hand panel groups
  the shown workspace's articles onto `total_pages` pages via the
  `page_number` column (added to both article tables by `_migrate`, copied
  by the daily reset). The three knobs live in `app/db/layout.py` — plain
  `settings` rows but with their own API (`/api/publication/settings`);
  `/api/settings` rejects those keys on purpose. The picker modal
  carries a live word-count meter: words already on the target page plus
  the current selection, projected against the quota/upper limit with the
  same color ramp as the sidebar bars (baseline embedded per page as
  `pageWordCounts`) — indigo under quota, green at 100%, heating through
  orange to red at `page_words + word_allowance`. Assigning is
  all-or-nothing (404 on any unknown id, 400 past `total_pages`), and
  shrinking `total_pages` un-places the orphans in both workspaces.
  **Export JSON**
  (`GET /api/publication/export`) builds settings + limits, pages with
  each article's heading/body split on a blank line, whole-file word
  counts, and each cover's URL / absolute path / pixel size — the payload
  the InDesign ExtendScript consumes.
- **Plugins browser** (`app/routes/plugins.py` + `templates/plugins.html`):
  a read-only view of the repo's `plugins/` folder, reached from the
  hamburger menu in `templates/base.html` (the header's secondary-
  destinations dropdown; `active_nav == "plugins"` highlights the entry).
  `GET /plugins?path=<folder>` lists a folder (dirs first, with size and
  modified time), `/plugins/view?path=<file>` previews a text file (binary
  or >`MAX_PREVIEW_BYTES` renders a note), and `/plugins/download?path=`
  serves a file as an attachment or a folder as a zip. Every request path
  is resolved against `PLUGINS_DIR` first — `..`, absolute paths and
  outward symlinks 404 instead of escaping the folder. Nothing writes.
- **Scheduled refresh** (`app/refresher.py`): `refresh_sources()` is shared by
  the manual `POST /api/refresh` and the background loop — scrape →
  `save_items` → `trim_articles` → stamp `refresh_last_run`. `run_due_refresh()`
  mirrors the AI Publish curator: stamp-before-scrape so a crashed run can't
  hot-retry, fail-soft (logged + notification, never kills the caller's loop).
- **Auto-scrape** (`app/autoscraper.py`): gated by the `auto_scrape` toggle and
  spaced by `auto_scrape_interval`. Each due tick scrapes *one* article — the
  oldest in the curation workspace without `public/<db>/articles/<id>.txt`, any
  status — through the same `enrich_accepted_article()` as the accept flow, so
  statuses never change. A page that fails is recorded once and remembered in
  a module-level `_FAILED` set (a restart retries it) so it can't block the
  oldest-first queue.
- **Curation auto-refresh** (`GET /api/state`): the open curation page polls
  a fingerprint of what the Review & Status panel shows (accepted /
  completed / rejected IDs + the AI Publish stamp) and reloads itself when
  it changes, so server-side rounds — AI Publish accepting + scraping, the
  completion loop, the daily reset — appear without a manual reload.
  Pending articles are deliberately excluded, so the background refresher
  adding stories never interrupts. While a round is in flight
  `curator.is_selecting()` is true, the meter renders "Curating…", and the
  reload waits until the round lands (one round → one refresh).
  Polling is **lazy**: the 3 s timer is created only when the AI Publish
  meter reaches "Due" (or when the page loads mid-round, seeded from
  `ai_publish_running`), and polls are skipped while the tab is hidden —
  nothing runs while the countdown is still ticking, so with AI Publish off
  the page never polls at all.

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
  keep that fixture if you add HTTP-dependent tests — and its autouse
  `_isolate_workspace` fixture rebinds every module's workspace dirs to a
  per-test temp dir, so `init_db()`'s daily reset (and any endpoint that
  writes content/covers) never touches the real `public/<db>/` workspace.
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
- The UI reads scraped article content from `public/<db>/articles/<id>.txt`
  (written by the accept endpoint) and covers from `public/<db>/covers/`.

## Conventions

- Commit messages use **gitmoji** prefixes with scopes, e.g.
  `🐛 fix(scrapers): handle multiple br tags`, `✨ feat(ui): ...`.
- Match the existing style: `from __future__ import annotations`,
  type hints everywhere, docstrings on non-obvious functions.
- Keep changes minimal — edit existing files rather than adding parallel
  helpers; new dependencies need justification.
- Do not commit `newsflow.db*`, `.venv/`, or `__pycache__/`. The whole
  scraped-data workspace is gitignored: the legacy flat dirs
  (`public/{articles,covers,publication}/`) and the per-database ones
  (`public/<db>/{articles,covers,publication}/`) alike — see `.gitignore`.
