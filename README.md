<p align="center">
  <img src="static/banner.svg" alt="News Flow — Aggregate · Review · Publish" width="100%"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.14-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/Tailwind_CSS-3.4-06B6D4?style=flat-square&logo=tailwindcss&logoColor=white" alt="Tailwind CSS"/>
  <img src="https://img.shields.io/badge/SQLite-3-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite"/>
  <img src="https://img.shields.io/badge/tests-passing-brightgreen?style=flat-square&logo=pytest&logoColor=white" alt="Tests"/>
  <img src="https://img.shields.io/badge/license-Common--Sense-8B5CF6?style=flat-square" alt="Common-Sense License"/>
</p>

---

## ✨ What is News Flow?

**News Flow** is a self-hosted, general-purpose news aggregation and curation tool. It scrapes articles from any website you configure, presents them in a clean review interface, and lets you accept, reject, or complete articles in a streamlined workflow.

### Key Features

- 🔄 **Caching feed** — Articles are scraped on demand, cached in SQLite, and trimmed so only fresh ones are kept
- 📰 **Multi-source aggregation** — Plug in any website via a simple, extensible scraper interface
- ✅ **Review workflow** — Accept articles to save content, reject to dismiss, or clear to restore
- ⏱️ **Completion timer** — Accepted articles auto-complete after a configurable interval
- 🖼️ **Cover image download** — Automatically fetches and stores cover images for accepted articles
- 📋 **Click-to-copy** — Copy headings, content, and image paths directly from the article view
- 🌐 **Playwright support** — Handles JS-heavy pages (SPAs, Next.js sites) with headless browser rendering
- 🧪 **Fully offline test suite** — Scrapers are tested against local HTML fixtures, no network required

---

## 🚀 Quick Start

### Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/news-flow.git
cd news-flow

# Install dependencies
uv sync

# Install Playwright browsers (for JS-rendered pages)
uv run playwright install chromium
```

### Running the App

```bash
uv run fastapi dev main.py
```

The app will be available at **http://localhost:8000**

---

## 📁 Project Structure

```
news-flow/
├── main.py                     # FastAPI app, startup/shutdown, completion loop
├── app/
│   ├── config.py               # Sources, timeouts, user agent
│   ├── models.py               # Pydantic models (NewsItem, ScrapeResponse)
│   ├── client.py               # HTTP client singleton
│   ├── utils.py                # HTML fetching, image download, text cleaning
│   ├── browser.py              # Playwright browser rendering
│   ├── scrapers/
│   │   ├── base.py             # Base scraper abstract class
│   │   ├── init.py             # Scraper registry & pipeline
│   │   └── <source>.py         # One scraper module per configured source
│   ├── routes/
│   │   └── scrape.py           # API + UI routes
│   └── db/                     # SQLAlchemy models, schema, queries
├── templates/                  # Jinja2 HTML templates
├── static/                     # Static assets (logo, banner, footer)
├── public/                     # Downloaded covers & saved article content
├── tests/
│   ├── fixtures/               # Offline HTML fixtures for testing
│   └── test_scrapers.py        # Scraper tests
├── scripts/
│   └── health_check.py         # Live smoke test
└── pyproject.toml              # Project config & dependencies
```

---

## 🔧 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Review UI (HTML) |
| `GET` | `/api/scrape` | Get news as JSON (optional `?source=`) |
| `POST` | `/api/refresh` | Re-scrape and refresh cached articles |
| `POST` | `/api/articles/{id}/accept` | Accept an article (starts completion timer) |
| `POST` | `/api/articles/{id}/reject` | Reject an article |
| `POST` | `/api/articles/{id}/clear` | Clear status (move back to pending) |
| `GET` | `/api/settings` | Get app settings |
| `POST` | `/api/settings` | Update settings |
| `GET` | `/article/{id}` | View accepted article content |

---

## 🧪 Testing

Tests run offline against local HTML fixtures — no network required.

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v
```

### Live Smoke Test

For testing your configured sources against the real websites (requires network):

```bash
uv run python scripts/health_check.py [source ...]
```

---

## ⚙️ Configuration

### Adding a New Source

1. Create a new scraper in `app/scrapers/` extending `BaseScraper`
2. Implement `scrape()` and optionally `scrape_article_page()`
3. Register it in `app/scrapers/init.py`
4. Add the source URL to `SOURCES` in `app/config.py`
5. Add HTML fixtures in `tests/fixtures/` for offline testing

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `completion_interval` | `600` | Seconds before accepted articles auto-complete |

---

## 🛠️ Tech Stack

- **Backend:** FastAPI, SQLAlchemy 2.0, httpx
- **Frontend:** Jinja2, Tailwind CSS
- **Database:** SQLite
- **Scraping:** BeautifulSoup4, Playwright
- **Testing:** pytest, pytest-asyncio
- **Package Management:** uv

---

## 📜 License

News Flow is released under the **Common-Sense License** — a custom license, fitting for a fully custom codebase.

This project was written end-to-end by an AI (Buffy, via Codebuff) at the direction of a human operator, so the license keeps the same spirit: simple, permissive, and honest. In short:

- **Use it** for anything — commercial, personal, educational, or the hobby project you abandon in three weeks
- **Modify it** freely; just don't pretend you wrote the original (keep a "Based on News Flow" notice)
- **Your changes are yours** — bugs you fix belong to you, bugs you introduce belong to you too
- **No warranty whatsoever** — it scrapes news sites; news sites change their markup at 2 a.m.; things will break

See the full text in [LICENSE](LICENSE).

---

<p align="center">
  <img src="static/footer.svg" alt="News Flow — self-hosted, Common-Sense licensed" width="100%"/>
</p>
