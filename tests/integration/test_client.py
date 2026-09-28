import asyncio
import time

import pytest
from agent_framework import SecretString
from psycopg import AsyncConnection, sql
from psycopg.errors import UndefinedTable
from psycopg_pool import AsyncConnectionPool, PoolClosed

from agent_framework_community_postgres import PostgresDocumentStore, PostgresPersistence
from agent_framework_community_postgres._client import ClientHandle, PostgresStorageError

pytestmark = pytest.mark.integration


async def test_owned_pool_runs_a_query_in_a_transaction(test_dsn: str) -> None:
    client = ClientHandle(SecretString(test_dsn), None)
    try:
        async with client.connection() as connection:
            cursor = await connection.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)
    finally:
        await client.close()


async def test_driver_errors_are_wrapped(test_dsn: str) -> None:
    client = ClientHandle(SecretString(test_dsn), None)
    try:
        with pytest.raises(PostgresStorageError) as info:
            async with client.connection() as connection:
                await connection.execute("SELECT * FROM cafp_missing_table")
        assert isinstance(info.value.__cause__, UndefinedTable)
    finally:
        await client.close()


async def test_borrowed_pool_is_opened_on_demand(test_dsn: str) -> None:
    pool = AsyncConnectionPool(test_dsn, open=False)
    try:
        client = ClientHandle(None, pool)
        async with client.connection() as connection:
            cursor = await connection.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)
        assert not pool.closed
        await client.close()
        assert not pool.closed
    finally:
        await pool.close()


async def test_closed_borrowed_pool_raises_storage_error(test_dsn: str) -> None:
    pool = AsyncConnectionPool(test_dsn, open=False)
    await pool.open()
    await pool.close()
    with pytest.raises(PostgresStorageError) as info:
        async with ClientHandle(None, pool).connection():
            pass
    assert isinstance(info.value.__cause__, PoolClosed)


async def test_borrowed_connection_is_used_and_not_closed(test_dsn: str) -> None:
    async with await AsyncConnection.connect(test_dsn, autocommit=True) as connection:
        client = ClientHandle(None, connection)
        async with client.connection() as borrowed:
            assert borrowed is connection
        await client.close()
        assert not connection.closed


_UNREACHABLE = [
    "postgresql://postgres:wrong@127.0.0.1:5433/agent_framework",
    "host=127.0.0.1 port=1 dbname=x",
]
_CLEAR = "Could not connect to PostgreSQL within 10 seconds"


async def _fail_once(handle: ClientHandle) -> None:
    started = time.monotonic()
    with pytest.raises(PostgresStorageError) as info:
        async with handle.connection():
            pass
    assert str(info.value).startswith(_CLEAR)
    assert time.monotonic() - started < 15


@pytest.mark.timeout(40)
@pytest.mark.parametrize("dsn", _UNREACHABLE)
async def test_unreachable_database_fails_fast_on_every_call(dsn: str) -> None:
    client = ClientHandle(SecretString(dsn), None)
    try:
        await _fail_once(client)
        await _fail_once(client)
        assert isinstance(client.client, AsyncConnectionPool)
        assert not client.client.closed
    finally:
        await client.close()


@pytest.mark.timeout(40)
async def test_concurrent_first_calls_both_fail_cleanly() -> None:
    client = ClientHandle(SecretString(_UNREACHABLE[1]), None)
    try:
        results = await asyncio.gather(_fail_once(client), _fail_once(client), return_exceptions=True)
        assert results == [None, None]
    finally:
        await client.close()


@pytest.mark.timeout(40)
async def test_child_of_an_unreachable_owned_handle_gets_the_clear_message() -> None:
    client = ClientHandle(SecretString(_UNREACHABLE[1]), None)
    try:
        await _fail_once(client.child())
    finally:
        await client.close()


async def test_concurrent_calls_over_one_borrowed_connection_are_serialized(test_dsn: str, schema: str) -> None:
    async with (
        await AsyncConnection.connect(test_dsn, autocommit=True) as connection,
        PostgresPersistence(application_id="tests", client=connection, schema=schema) as hub,
    ):
        await hub.migrate()
        separate = PostgresDocumentStore(application_id="tests", collection="threads", client=connection, schema=schema)
        stores = [hub.document_store(collection="threads"), separate]
        results = await asyncio.gather(
            *(stores[n % 2].put(scope="s", key=f"k{n}", payload={"n": n}) for n in range(10))
        )
        assert results == [1] * 10
    async with await AsyncConnection.connect(test_dsn, autocommit=True) as other:
        cursor = await other.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, "af_documents")))
        assert await cursor.fetchone() == (10,)
