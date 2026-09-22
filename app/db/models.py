"""SQLAlchemy ORM models for the News Flow database."""

from sqlalchemy import Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.engine import Base


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("idx_articles_source", "source"),
        Index("idx_articles_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String)
    image_url: Mapped[str | None] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    scraped_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=func.datetime("now")
    )
    status: Mapped[str | None] = mapped_column(String)
    accepted_at: Mapped[str | None] = mapped_column(String)
    accepted_order: Mapped[int | None] = mapped_column(Integer)
    cover_file: Mapped[str | None] = mapped_column(String)

    # JSON list of WordPress category IDs chosen by Gemini before publishing;
    # NULL until the publisher marks them.
    wp_category_ids: Mapped[list[int] | None] = mapped_column(JSON)


class WpCategory(Base):
    """A category (or tag, when taxonomy='post_tag') synced from WordPress."""

    __tablename__ = "wp_terms"
    __table_args__ = (Index("idx_wp_terms_taxonomy", "taxonomy"),)

    # WordPress term IDs; synced from the site, never generated locally. The
    # taxonomy is part of the key so the two taxonomies can never collide.
    wp_id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    synced_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=func.datetime("now")
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
