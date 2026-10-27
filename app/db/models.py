"""SQLAlchemy ORM models for the News Flow database."""

from sqlalchemy import Boolean, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.engine import Base


class _ArticleFields:
    """Columns shared by the curation and publication article tables.

    The publication workspace mirrors the curation one column for column —
    including ``id``, which is copied verbatim by the daily reset so the
    ``<id>.txt`` content file names keep matching after the move. Defining
    the columns once guarantees the two tables stay in step (``_migrate``
    upgrades both).
    """

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


class Article(_ArticleFields, Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("idx_articles_source", "source"),
        Index("idx_articles_status", "status"),
    )


class PublicationArticle(_ArticleFields, Base):
    """The previous day's articles, archived by the daily workspace reset.

    Written by ``_reset_daily_workspace`` (which replaces the whole archive
    on every new day) and read by the ``/publication`` page. It is an exact
    mirror of :class:`Article`, ids included.
    """

    __tablename__ = "publication_articles"


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


class Notification(Base):
    """A log entry surfaced in the UI (e.g. Gemini/API failures)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # One of the NOTIF_* levels from app.db.constants.
    level: Mapped[str] = mapped_column(String, nullable=False)
    # Component that produced it, e.g. "gemini".
    source: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False)
    article_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=func.datetime("now")
    )
    # Unseen flag behind the bell badge: False until the notification button
    # is clicked, which marks every unread row as read in one go.
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
