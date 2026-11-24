import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, event, String, Text, Integer, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from . import config

STATUSES = ("scheduled", "processing", "published", "failed")

def now() -> datetime:
    """Naive UTC. Every datetime in the DB is UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

if config.DATABASE_URL.startswith("sqlite:///"):
    path = config.DATABASE_URL.replace("sqlite:///", "", 1)
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)

engine = create_engine(config.DATABASE_URL, connect_args={"timeout": 30} if "sqlite" in config.DATABASE_URL else {})
if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

Session = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    linkedin_sub: Mapped[str] = mapped_column(String(64), unique=True)
    person_urn: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255), default="")
    access_token_enc: Mapped[str] = mapped_column(Text)            # Fernet ciphertext
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # set right before the HTTP call; non-null on a re-claim => previous outcome unknown
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    linkedin_post_urn: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    user: Mapped[User] = relationship()
    __table_args__ = (
        # duplicate-submission guard: same user + same text + same time
        UniqueConstraint("user_id", "content_hash", "scheduled_at", name="uq_no_dup_submission"),
        Index("ix_due", "status", "next_attempt_at"),
    )

class PostEvent(Base):
    """Append-only audit log per post."""
    __tablename__ = "post_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("scheduled_posts.id"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)

def init_db():
    Base.metadata.create_all(engine)
