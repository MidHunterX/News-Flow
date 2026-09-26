"""One-shot offline simulation of the duplicate-publish race.

Runs two concurrent publish triggers (the background loop + a UI reload) over
a throwaway SQLite DB while WordPress POSTs take 300ms. Before the fix both
triggers returned the same due article -> 2 posts. After the fix, the atomic
claim means exactly 1 post.

Usage: uv run python scripts/race_check.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import publisher
from app.db import articles as articles_mod
from app.db.constants import STATUS_ACCEPTED
from app.db.models import Article, Base
from app.models import NewsItem


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    engine = create_engine(f"sqlite:///{tmp / 'race.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    articles_mod.SessionLocal = session_factory

    posts: list[int] = []

    async def fake_publish(article: NewsItem) -> str | None:
        await asyncio.sleep(0.3)  # simulate media upload + post creation
        posts.append(article.id)
        return "https://wp.example.com/?p=1"

    publisher.publish_article = fake_publish  # type: ignore[assignment]

    # Seed an article accepted 700s ago (interval is 600s -> due).
    past = "2020-01-01T00:00:00+00:00"
    with session_factory() as session:
        session.add(
            Article(
                title="t",
                url="https://example.com/a",
                description="d",
                published_at="2026-09-19",
                source="kaumudi",
                status=STATUS_ACCEPTED,
                accepted_at=past,
                accepted_order=1,
            )
        )
        session.commit()

    # Two triggers race: the 2s background loop AND a UI page load.
    await asyncio.gather(
        publisher.publish_due_articles(600),
        publisher.publish_due_articles(600),
    )

    print(f"posts created: {len(posts)}")
    assert len(posts) == 1, f"EXPECTED 1 POST, GOT {len(posts)} -> DUPLICATE!"
    print("OK: exactly one post — race fixed.")


if __name__ == "__main__":
    asyncio.run(main())
