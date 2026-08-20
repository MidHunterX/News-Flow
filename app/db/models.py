"""SQLAlchemy ORM models for the News Flow database."""

from sqlalchemy import Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

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


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
