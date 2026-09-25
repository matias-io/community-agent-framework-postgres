import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
from agent_framework import SecretString
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._migrations import migrate

if sys.platform == "win32":

    def pytest_asyncio_loop_factories(
        config: pytest.Config, item: pytest.Item
    ) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
        """Async psycopg cannot use Windows' default ProactorEventLoop."""
        return {"selector": asyncio.SelectorEventLoop}


TEST_DSN_ENV = "POSTGRES_TEST_CONNECTION_STRING"


@pytest.fixture
def test_dsn() -> str:
    dsn = os.getenv(TEST_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"Set {TEST_DSN_ENV} to run integration tests.")
    return dsn


@pytest.fixture
async def schema(test_dsn: str) -> AsyncIterator[str]:
    name = f"cafp_test_{uuid4().hex[:12]}"
    async with await AsyncConnection.connect(test_dsn, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def names(schema: str) -> TableNames:
    return TableNames(schema=schema, prefix="af_")


@pytest.fixture
async def client(test_dsn: str) -> AsyncIterator[ClientHandle]:
    instance = ClientHandle(SecretString(test_dsn), None)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def migrated(client: ClientHandle, names: TableNames) -> TableNames:
    await migrate(client, names)
    return names


@pytest.fixture
async def persistence(test_dsn: str, schema: str) -> AsyncIterator[PostgresPersistence]:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as hub:
        await hub.migrate()
        yield hub
