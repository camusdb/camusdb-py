import os
import uuid

import pytest

import camusdb

ENDPOINT = os.environ.get("CAMUSDB_TEST_ENDPOINT")


def pytest_collection_modifyitems(config, items):
    if ENDPOINT:
        return
    skip = pytest.mark.skip(reason="set CAMUSDB_TEST_ENDPOINT to run the integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def endpoint():
    return ENDPOINT


@pytest.fixture(scope="session")
def database(endpoint):
    name = "pytest_" + uuid.uuid4().hex[:12]
    admin = camusdb.connect(endpoint=endpoint, database=name, autocommit=True)
    admin.create_database(if_not_exists=True)
    yield name
    try:
        admin.drop_database()
    finally:
        admin.close()


@pytest.fixture
def conn(endpoint, database):
    connection = camusdb.connect(endpoint=endpoint, database=database)
    yield connection
    connection.close()


@pytest.fixture
def table(endpoint, database):
    """A fresh table with one column of every type, for each test."""
    name = "t_" + uuid.uuid4().hex[:10]
    with camusdb.connect(endpoint=endpoint, database=database, autocommit=True) as admin:
        admin.execute(
            f"""
            CREATE TABLE {name} (
                id      OID PRIMARY KEY NOT NULL,
                name    STRING(64) NOT NULL,
                year    INT64,
                price   FLOAT64,
                score   FLOAT32,
                total   NUMERIC,
                enabled BOOL,
                ref     UUID,
                payload BYTES,
                day     DATE,
                seen    DATETIME,
                tags    ARRAY(INT64)
            )
            """
        )
    return name


@pytest.fixture
def anyio_backend():
    return "asyncio"
