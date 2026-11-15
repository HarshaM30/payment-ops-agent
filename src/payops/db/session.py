"""Engine and session factory (sync SQLAlchemy 2 + psycopg 3)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from payops.config import get_settings


def make_engine(url: str) -> Engine:
    settings = get_settings()
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
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
        yield session


def check_db(engine: Engine | None = None) -> None:
    """Raise if the database is unreachable."""
    with (engine or get_engine()).connect() as conn:
        conn.execute(text("SELECT 1"))
