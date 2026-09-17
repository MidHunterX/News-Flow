"""Standalone health-check script for live scraper smoke testing.

Moved out of the pytest suite so CI/automated tests no longer hit live
websites. Run manually when you want to verify the scrapers still work
against the real sources:

    python scripts/health_check.py            # all sources
    python scripts/health_check.py kaumudi    # one or more specific sources

For each active source the script:
1. Fetches the listing page and parses articles.
2. Picks the latest (first) article.
3. Fetches and scrapes the article page for heading, content and cover image.
4. Downloads the cover image to public/covers/.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make "app" importable when running from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import fetch_rendered_html  # noqa: E402
from app.client import HttpClient  # noqa: E402
from app.config import SOURCES  # noqa: E402
from app.scrapers.init import SCRAPERS  # noqa: E402
from app.utils import download_image, fetch_html  # noqa: E402


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SourceReport:
    source: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> StepResult:
        step = StepResult(name=name, ok=ok, detail=detail)
        self.steps.append(step)
        return step


# ---------------------------------------------------------------------------
# Checks (one per pipeline stage, mirroring the former live tests)
# ---------------------------------------------------------------------------

async def check_listing_page(report: SourceReport) -> list | None:
    """Fetch and parse the listing page. Returns parsed items or None."""
    source = report.source
    scraper = SCRAPERS.get(source)
    if scraper is None:
        report.add("register scraper", ok=False, detail=f"No scraper registered for '{source}'")
        return None
    report.add("register scraper", ok=True)

    list_url = SOURCES[source]
    try:
        if scraper.needs_browser:
            html = await fetch_rendered_html(
                list_url,
                wait_selector="div.main-news, div.cat-news, div.other-news",
            )
        else:
            html = await fetch_html(list_url)
    except Exception as exc:
        report.add("fetch listing page", ok=False, detail=f"{list_url} -> {exc}")
        return None

    if not html:
        report.add("fetch listing page", ok=False, detail=f"{list_url} returned empty HTML")
        return None
    report.add("fetch listing page", ok=True, detail=f"{len(html)} chars from {list_url}")

    try:
        items = await scraper.scrape(html)
    except Exception as exc:
        report.add("parse listing page", ok=False, detail=f"{type(exc).__name__}: {exc}")
        report.add("html snippet", ok=False, detail=html[:500])
        return None

    if not items:
        report.add(
            "parse listing page",
            ok=False,
            detail=(
                f"0 articles parsed from {list_url} (html={len(html)} chars). "
                f"Check CSS selectors in {type(scraper).__name__}.scrape()."
            ),
        )
        report.add("html snippet", ok=False, detail=html[:500])
        return None

    bad = [
        (idx, item)
        for idx, item in enumerate(items)
        if not item.title or not item.url
    ]
    if bad:
        idx, item = bad[0]
        report.add(
            "validate articles",
            ok=False,
            detail=(
                f"Article {idx} missing {'title' if not item.title else 'URL'} "
                f"(title={item.title!r}, url={item.url})"
            ),
        )
        return None

    report.add("parse listing page", ok=True, detail=f"{len(items)} articles parsed")
    return items


async def check_article_page(report: SourceReport, items: list) -> object | None:
    """Fetch and scrape the latest article page. Returns scraped content or None."""
    source = report.source
    scraper = SCRAPERS[source]
    latest = items[0]

    if not latest.url:
        report.add("pick latest article", ok=False, detail="Latest article has no URL")
        return None
    report.add(
        "pick latest article",
        ok=True,
        detail=f"title={latest.title!r} url={latest.url}",
    )

    try:
        if scraper.needs_browser:
            article_html = await fetch_rendered_html(latest.url, wait_selector="h1")
        else:
            article_html = await fetch_html(latest.url)
    except Exception as exc:
        report.add("fetch article page", ok=False, detail=f"{latest.url} -> {exc}")
        return None

    if not article_html:
        report.add("fetch article page", ok=False, detail=f"{latest.url} returned empty HTML")
        return None
    report.add("fetch article page", ok=True, detail=f"{len(article_html)} chars")

    try:
        scraped = await scraper.scrape_article_page(article_html)
    except Exception as exc:
        report.add("scrape article page", ok=False, detail=f"{type(exc).__name__}: {exc}")
        report.add("html snippet", ok=False, detail=article_html[:500])
        return None

    if scraped is None:
        report.add(
            "scrape article page",
            ok=False,
            detail=(
                f"scrape_article_page() returned None for {latest.url} "
                f"(html={len(article_html)} chars). Selectors may not match."
            ),
        )
        report.add("html snippet", ok=False, detail=article_html[:800])
        return None

    problems = []
    if not scraped.heading:
        problems.append("heading is empty")
    if not scraped.content:
        problems.append("content is empty")
    if scraped.cover_path and not scraped.cover_path.startswith("http"):
        problems.append(f"cover_path is not an absolute URL: {scraped.cover_path!r}")
    if problems:
        report.add("scrape article page", ok=False, detail="; ".join(problems))
        return None

    report.add(
        "scrape article page",
        ok=True,
        detail=(
            f"heading={scraped.heading!r} "
            f"content={len(scraped.content)} chars "
            f"cover={scraped.cover_path}"
        ),
    )
    return scraped


async def check_cover_image(report: SourceReport, scraped) -> None:
    """Download the cover image of the scraped article, if present."""
    source = report.source
    if not scraped.cover_path:
        report.add(
            "download cover image",
            ok=True,
            detail="No cover image URL found in article (nothing to download)",
        )
        return

    try:
        local_path = await download_image(scraped.cover_path)
    except Exception as exc:
        report.add("download cover image", ok=False, detail=f"{scraped.cover_path} -> {exc}")
        return

    if local_path is None:
        report.add(
            "download cover image",
            ok=False,
            detail=f"download_image() returned None for {scraped.cover_path}",
        )
        return

    saved = Path(local_path)
    if not saved.exists():
        report.add("download cover image", ok=False, detail=f"File not created: {local_path}")
        return
    size = saved.stat().st_size
    if size == 0:
        report.add("download cover image", ok=False, detail=f"File is empty: {local_path}")
        return

    report.add(
        "download cover image",
        ok=True,
        detail=f"saved {local_path} ({size} bytes)",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_health_check(source_names: list[str]) -> int:
    """Run the full live check. Returns the number of failed sources."""
    active = list(SOURCES.keys())
    unknown = [name for name in source_names if name not in SCRAPERS]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}. Known: {', '.join(SCRAPERS)}")
        return len(unknown)

    targets = source_names or active
    reports: list[SourceReport] = []

    for name in targets:
        report = SourceReport(source=name)
        reports.append(report)
        print(f"\n--- health check: {name} ---")
        items = await check_listing_page(report)
        if items:
            scraped = await check_article_page(report, items)
            if scraped:
                await check_cover_image(report, scraped)

    failed = [r for r in reports if not r.ok]

    print("\n" + "=" * 72)
    print("HEALTH CHECK SUMMARY")
    print("=" * 72)
    for report in reports:
        status = "OK  " if report.ok else "FAIL"
        print(f"  [{status}] {report.source}")
        for step in report.steps:
            if not step.ok or step.name == "html snippet":
                print(f"      {step.name}: {step.detail}")
    print("=" * 72)
    print(f"{len(reports) - len(failed)}/{len(reports)} sources healthy")
    return len(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live scraper health check")
    parser.add_argument(
        "sources",
        nargs="*",
        help="Optional list of source names to check (default: all active sources)",
    )
    args = parser.parse_args()

    # Each source check gets a fresh HTTP client bound to its own event loop.
    HttpClient._instance = None
    try:
        failed = asyncio.run(run_health_check(args.sources))
    finally:
        HttpClient._instance = None

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
