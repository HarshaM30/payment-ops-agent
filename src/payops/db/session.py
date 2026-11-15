"""Engine and session factory (sync SQLAlchemy 2 + psycopg 3)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from payops.config import get_settings


def make_engine(url: str) -> Engine:
    settings = get_settings()
    connect_args: dict[str, object] = {"connect_timeout": settings.db_connect_timeout_seconds}
    if settings.db_statement_timeout_ms:
        # Server-side cap so one slow query can't hold a pooled connection forever.
        connect_args["options"] = f"-c statement_timeout={settings.db_statement_timeout_ms}"
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Creating a Session does not open a connection until first use."""
    with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def check_db(engine: Engine | None = None) -> None:
    """Raise if the database is unreachable."""
    with (engine or get_engine()).connect() as conn:
        conn.execute(text("SELECT 1"))
