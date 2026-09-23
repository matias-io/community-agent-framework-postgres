import pytest
from agent_framework import SecretString
from psycopg import AsyncConnection
from psycopg.errors import UndefinedTable
from psycopg_pool import AsyncConnectionPool, PoolClosed

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
