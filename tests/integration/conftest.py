"""Integration fixtures: a real Postgres database, migrated with Alembic.

Each test runs inside a transaction that is rolled back afterwards, so tests are
isolated even when the code under test calls ``session.commit()``.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from payops.api.routes.health import get_db_check
from payops.api.routes.runs import get_run_executor
from payops.config import get_settings
from payops.db.admin import ensure_database, upgrade_head
from payops.db.session import check_db, get_session
from payops.main import create_app
from tests.conftest import TEST_API_KEY


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


def test_database_url() -> str:
    url = get_settings().test_database_url
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set (see .env.example)")
    return url


test_database_url.__test__ = False  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = test_database_url()
    ensure_database(url)
    eng = create_engine(url)
    upgrade_head(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    trans = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def client(engine: Engine, db_session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_db_check] = lambda: lambda: check_db(engine)
    # Runs are executed explicitly by the tests that need it, never against the dev DB.
    app.dependency_overrides[get_run_executor] = lambda: lambda run_id: None
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as c:
        yield c
