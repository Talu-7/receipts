"""Database models and session handling (SQLAlchemy 2.0; SQLite for dev, Postgres in production)."""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event,
                        select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(n: int = 6) -> str:
    return secrets.token_hex(n)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id(8))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # None: magic-link only
    plan: Mapped[str] = mapped_column(String(32), default="free")
    plan_status: Mapped[str] = mapped_column(String(32), default="active")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id(6))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="default")
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MagicLink(Base):
    __tablename__ = "magic_links"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Check(Base):
    """One reproduction request. It doubles as the queue row (status queued -> running -> done|failed)."""
    __tablename__ = "checks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id(6))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    arxiv_url: Mapped[str] = mapped_column(String(500))
    claim_override: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    seeds: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # caps frozen at submission time from the account's plan
    gpu_minutes_cap: Mapped[float] = mapped_column(Float, default=30.0)
    wall_seconds_cap: Mapped[int] = mapped_column(Integer, default=2700)
    period: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM", for quotas
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CheckEvent(Base):
    __tablename__ = "check_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[str] = mapped_column(ForeignKey("checks.id"), index=True)
    t: Mapped[float] = mapped_column(Float)
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    msg: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class UsageRecord(Base):
    """Metered compute for one finished check (one row per check)."""
    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    check_id: Mapped[str] = mapped_column(ForeignKey("checks.id"), unique=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    job_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    wall_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    gpu_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StripeEvent(Base):
    """Processed webhook ids, so a redelivered event is applied once."""
    __tablename__ = "stripe_events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    type: Mapped[str] = mapped_column(String(80))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Database:
    def __init__(self, url: str):
        kw: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.url = url
        self.engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _):  # API, worker and per-check children all write
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def get_user_by_email(s: Session, email: str) -> User | None:
    return s.scalar(select(User).where(User.email == email.strip().lower()))
