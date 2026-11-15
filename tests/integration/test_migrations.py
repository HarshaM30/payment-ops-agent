from alembic import command
from sqlalchemy import create_engine, inspect, make_url

from payops.db.base import Base
from tests.integration.conftest import (
    alembic_config,
    drop_database,
    ensure_database,
    test_database_url,
)


def test_upgrade_downgrade_upgrade_and_no_drift() -> None:
    url = make_url(test_database_url())
    url = url.set(database=f"{url.database}_migrations").render_as_string(hide_password=False)
    drop_database(url)
    ensure_database(url)
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            cfg = alembic_config(conn)
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "base")
            assert set(inspect(conn).get_table_names()) == {"alembic_version"}
            command.upgrade(cfg, "head")
            assert set(Base.metadata.tables) <= set(inspect(conn).get_table_names())
            command.check(cfg)  # raises if models and migrations disagree
    finally:
        eng.dispose()
        drop_database(url)
