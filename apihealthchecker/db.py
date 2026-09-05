"""Database access layer.

SQLAlchemy so the same code runs against a file-backed SQLite database (local
dev, and the Fly volume in production) or an in-memory one (tests), controlled
entirely by DATABASE_URL.

Three tables:
- monitors: what to check, and how often.
- check_results: every result ever recorded, the history behind the sparklines.
- scheduler_lock: a single row used as a cross-process lease so exactly one
  gunicorn worker runs the scheduler. See scheduler.py for why.
"""
import os
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import JSON


def _default_database_url() -> str:
    """Resolve the database URL from the environment.

    DATABASE_URL wins if set. Otherwise DB_PATH names a SQLite file, defaulting
    to ./local.db so a fresh clone runs with no configuration at all. On Fly,
    DB_PATH points inside the mounted volume (see fly.toml), which is the whole
    reason it is a separate knob: the deploy sets a path, not a URL scheme.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return f"sqlite:///{os.environ.get('DB_PATH', 'local.db')}"


DATABASE_URL = _default_database_url()

_is_sqlite = DATABASE_URL.startswith("sqlite")
_is_memory = DATABASE_URL in ("sqlite:///:memory:", "sqlite://")

engine = create_engine(
    DATABASE_URL,
    # The scheduler thread and the request threads share this engine, so SQLite
    # must not enforce its same-thread rule.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # An in-memory database lives in its connection, so tests need every caller
    # on the same one or each session would see an empty schema.
    poolclass=StaticPool if _is_memory else None,
)
if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        """Turn on foreign key enforcement, which SQLite leaves off by default.

        Without this the ondelete="CASCADE" on check_results is a comment: the
        schema declares it, SQLAlchemy's passive_deletes trusts the database to
        act on it, and SQLite quietly ignores it. Deleting a monitor would then
        leave its results behind forever, orphaned rows in a table that only
        grows. The pragma is per connection, so it has to be set on connect
        rather than once at startup.
        """
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    Everything stored is UTC. Naive local timestamps are the reason a monitoring
    tool disagrees with itself about when an incident started.
    """
    return datetime.now(UTC)


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    target = Column(String(2000), nullable=False)
    type = Column(String(20), nullable=False, default="http")
    interval_seconds = Column(Integer, nullable=False, default=60)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    results = relationship(
        "CheckResultRow",
        back_populates="monitor",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "target": self.target,
            "type": self.type,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "created_at": _iso(self.created_at),
        }


class CheckResultRow(Base):
    """A single recorded check outcome.

    Named CheckResultRow rather than CheckResult so it never shadows the
    engine's dataclass of that name: one is what a check returns, the other is
    what got stored, and confusing them is how a schema change quietly breaks
    the check engine.
    """

    __tablename__ = "check_results"

    id = Column(Integer, primary_key=True)
    monitor_id = Column(
        Integer, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
    )
    status = Column(String(20), nullable=False)
    message = Column(Text, nullable=False, default="")
    detail = Column(JSON, nullable=False, default=dict)
    # Null for a passing check: a category on an OK result would be noise, and
    # storing one would make "has a category" stop meaning "needs attention".
    category = Column(String(40), nullable=True)
    severity = Column(String(20), nullable=True)
    latency_ms = Column(Float, nullable=True)
    checked_at = Column(DateTime, nullable=False, default=utcnow)

    monitor = relationship("Monitor", back_populates="results")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "monitor_id": self.monitor_id,
            "status": self.status,
            "message": self.message,
            "detail": self.detail or {},
            "category": self.category,
            "severity": self.severity,
            "latency_ms": self.latency_ms,
            "checked_at": _iso(self.checked_at),
        }


# Every history query and every status rollup reads the newest rows for one
# monitor. Without this index that is a full scan of a table which, by design,
# only ever grows.
Index(
    "ix_check_results_monitor_checked_at",
    CheckResultRow.monitor_id,
    CheckResultRow.checked_at.desc(),
)


class SchedulerLock(Base):
    """A single-row lease identifying which process owns the scheduler.

    Kept in the database rather than in process memory precisely because
    process memory is what does not survive being run under several gunicorn
    workers. See scheduler.py.
    """

    __tablename__ = "scheduler_lock"

    id = Column(Integer, primary_key=True)
    owner = Column(String(100), nullable=False)
    heartbeat_at = Column(DateTime, nullable=False, default=utcnow)


def _iso(value: datetime | None) -> str | None:
    """Render a stored datetime as a UTC ISO-8601 string.

    SQLite gives back naive datetimes even when timezone-aware ones went in, so
    the tz is reattached here rather than trusted from the driver. Without this
    the UI would read every timestamp as local and show wrong relative times.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def init_db() -> None:
    """Create the schema if it is not there.

    In anything beyond a portfolio service this would be alembic migrations
    rather than create_all at boot.
    """
    Base.metadata.create_all(bind=engine)


def check_db_connection() -> bool:
    """True if the database answers a trivial query.

    Deliberately broad in what it catches: /health must report a degraded
    dependency, never raise its way into a 500. A health endpoint that can
    itself fail is not a health endpoint.
    """
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False
