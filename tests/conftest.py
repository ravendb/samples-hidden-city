import os

import httpx
import pytest
from pyravendb.store.document_store import DocumentStore

TEST_DB = "hidden-city-test"


def pytest_addoption(parser):
    parser.addoption(
        "--require-ravendb",
        action="store_true",
        default=False,
        help="Fail the test run if RavenDB is not reachable (default: skip)",
    )


@pytest.fixture(scope="session")
def ravendb_url() -> str:
    return os.getenv("RAVENDB_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def ravendb_store(request, ravendb_url):
    try:
        httpx.get(f"{ravendb_url}/alive", timeout=2.0).raise_for_status()
    except Exception:
        if request.config.getoption("--require-ravendb"):
            pytest.fail("RavenDB not reachable and --require-ravendb was set")
        pytest.skip("RavenDB not reachable")

    store = DocumentStore(urls=[ravendb_url], database=TEST_DB)
    store.initialize()
    yield store
    store.dispose()


@pytest.fixture
def ravendb_session(ravendb_store):
    with ravendb_store.open_session() as session:
        yield session
