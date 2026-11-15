"""Database admin helpers shared by the test fixtures and the eval runner:
create/drop a database, and migrate one to the latest Alembic revision."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, make_url, text

ROOT = Path(__file__).resolve().parents[3]


def _admin_engine(url: str) -> Engine:
    return create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")


def ensure_database(url: str) -> None:
    """Create the database named in ``url`` if it does not exist yet."""
    name = make_url(url).database
    admin = _admin_engine(url)
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def drop_database(url: str) -> None:
    admin = _admin_engine(url)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)'))
    finally:
        admin.dispose()


def alembic_config(connection: Connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    cfg.attributes["skip_logging_config"] = True
    return cfg


def upgrade_head(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
