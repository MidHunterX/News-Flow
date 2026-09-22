"""Live smoke test for the WordPress terms sync + Gemini categorization chain.

Run manually (hits the real WordPress site and, when configured, the Gemini
API — never run in CI):

    uv run python scripts/publish_check.py                # sync + categorize
    uv run python scripts/publish_check.py --publish      # also POST a draft
    uv run python scripts/publish_check.py --publish --keep   # keep the draft

For each stage the script:
1. Checks WordPress credentials and syncs all categories/tags into wp_terms.
2. Sends a sample article plus the synced terms to Gemini and reports the
   matched WordPress categories.
3. With --publish, creates a DRAFT post carrying those categories (mirroring
   the publisher payload), verifies it, then deletes it unless --keep.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make "app" importable when running from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.client import HttpClient  # noqa: E402
from app.config import (GEMINI_API_KEY, WORDPRESS_APP_PASSWORD,  # noqa: E402
                        WORDPRESS_URL, WORDPRESS_USERNAME)
from app.db import get_categories, get_tags, init_db  # noqa: E402
from app.gemini import suggest_categories  # noqa: E402
from app.models import NewsItem  # noqa: E402
from app.wordpress import sync_terms  # noqa: E402

# Deliberately unambiguous sample: a cricket match should trip any sports
# category on a news site, making "no match" a meaningful smoke failure.
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
    source="smoke-test",
)


# ---------------------------------------------------------------------------
# Result bookkeeping (same shape as scripts/health_check.py)
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> StepResult:
        step = StepResult(name=name, ok=ok, detail=detail)
        self.steps.append(step)
        return step


def print_report(report: Report) -> None:
    print("\n" + "=" * 72)
    print("PUBLISH CHECK SUMMARY")
    print("=" * 72)
    for step in report.steps:
        status = "OK  " if step.ok else "FAIL"
        print(f"  [{status}] {step.name}")
        if step.detail:
            print(f"      {step.detail}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


async def check_terms_sync(report: Report) -> bool:
    """Sync WordPress terms into the DB. Returns True when categories exist."""
    missing = [
        name
        for name, value in (
            ("WORDPRESS_URL", WORDPRESS_URL),
            ("WORDPRESS_USERNAME", WORDPRESS_USERNAME),
            ("WORDPRESS_APP_PASSWORD", WORDPRESS_APP_PASSWORD),
        )
        if not value
    ]
    if missing:
        report.add("wordpress config", ok=False,
                   detail=f"Missing env vars: {', '.join(missing)} (.env)")
        return False
    report.add("wordpress config", ok=True, detail=WORDPRESS_URL)

    try:
        await sync_terms()
    except Exception as exc:
        report.add("terms sync", ok=False,
                   detail=f"{type(exc).__name__}: {exc}")
        return False

    categories, tags = await get_categories(), await get_tags()
    if not categories:
        report.add("terms sync", ok=False,
                   detail="0 categories stored — check the site's REST API")
        return False
    report.add(
        "terms sync",
        ok=True,
        detail=f"{len(categories)} categories, {len(tags)} tags stored in wp_terms",
    )
    return True


async def check_gemini(report: Report) -> list[int]:
    """Categorize the sample article. Returns the matched category IDs."""
    if not GEMINI_API_KEY:
        report.add("gemini config", ok=False,
                   detail="GEMINI_API_KEY not set — categorization disabled")
        return []

    matched = await suggest_categories(
        SAMPLE, SAMPLE.title, SAMPLE.description
    )
    if not matched:
        report.add(
            "gemini categorization",
            ok=False,
            detail="No categories matched for an unambiguous sports article. "
                   "Check GEMINI_API_KEY validity and the synced term names.",
        )
        return []

    names = {c.wp_id: c.name for c in await get_categories()}
    pretty = ", ".join(f"{wp_id}={names.get(wp_id, '?')}" for wp_id in matched)
    report.add("gemini categorization", ok=True, detail=f"matched: {pretty}")
    return matched


async def check_draft_publish(report: Report, category_ids: list[int],
                              keep: bool) -> None:
    """Create a draft post with the matched categories, then clean it up."""
    from app.publisher import _api_url, _auth_header, _post_fields

    fields = _post_fields(SAMPLE, SAMPLE.title, SAMPLE.description,
                          featured_media=None)
    fields["status"] = "draft"  # never publish from a smoke test
    if category_ids:
        fields["categories"] = category_ids

    from app.client import get_client

    client = await get_client()
    try:
        resp = await client.post(_api_url("/posts"), headers=_auth_header(),
                                 json=fields)
        resp.raise_for_status()
        post = resp.json()
    except Exception as exc:
        report.add("draft post", ok=False, detail=f"{type(exc).__name__}: {exc}")
        return

    post_id, link = post.get("id"), post.get("link")
    report.add("draft post", ok=True,
               detail=f"id={post_id} link={link} categories={fields.get('categories')}")
    if keep:
        report.add("cleanup", ok=True, detail=f"--keep: draft {post_id} left in place")
        return
    try:
        del_resp = await client.delete(f"{_api_url('/posts')}/{post_id}?force=true",
                                       headers=_auth_header())
        del_resp.raise_for_status()
        report.add("cleanup", ok=True, detail=f"deleted draft {post_id}")
    except Exception as exc:
        report.add("cleanup", ok=False,
                   detail=f"could not delete draft {post_id}: {exc} (remove it in wp-admin)")


async def run(publish: bool, keep: bool) -> int:
    report = Report()
    init_db()

    terms_ok = await check_terms_sync(report)
    if terms_ok:
        ids = await check_gemini(report)
        if publish:
            await check_draft_publish(report, ids, keep)

    print_report(report)
    failed = [s for s in report.steps if not s.ok]
    print(f"{len(report.steps) - len(failed)}/{len(report.steps)} checks passed")
    return len(failed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live WordPress terms sync + Gemini categorization smoke test"
    )
    parser.add_argument("--publish", action="store_true",
                        help="also create a DRAFT post carrying the matched categories")
    parser.add_argument("--keep", action="store_true",
                        help="with --publish: keep the draft instead of deleting it")
    args = parser.parse_args()

    HttpClient._instance = None
    try:
        failed = asyncio.run(run(args.publish, args.keep))
    finally:
        HttpClient._instance = None
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
