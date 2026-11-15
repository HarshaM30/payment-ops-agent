from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from payops.db.session import get_session
from payops.main import create_app


class _NoDB:
    """Stands in for a Session in unit tests; any use is a test bug."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unit test touched the database (Session.{name})")


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = lambda: _NoDB()
    return app


@pytest.fixture
def raw_client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
