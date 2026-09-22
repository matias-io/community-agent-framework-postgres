import asyncio

import pytest
from agent_framework import SecretString
from psycopg import sql

from agent_framework_community_postgres._client import TableNames, _Client
from agent_framework_community_postgres._migrations import current_version, migrate, pending_versions

pytestmark = pytest.mark.integration


async def test_migrate_is_idempotent_and_records_versions(client: _Client, names: TableNames) -> None:
    assert await current_version(client, names) == 0
    assert await pending_versions(client, names) == [1]
    first = await migrate(client, names)
    assert first.applied == (1,)
    assert first.current == 1
    second = await migrate(client, names)
    assert second.applied == ()
    assert await pending_versions(client, names) == []
    async with client.connection() as connection:
        cursor = await connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
            (names.schema,),
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert tables == sorted(
        f"af_{name}"
        for name in (
            "migrations",
            "history_messages",
            "sessions",
            "checkpoints",
            "thread_snapshots",
            "documents",
            "leases",
        )
    )


async def test_prefix_and_schema_are_honoured(client: _Client, schema: str) -> None:
    names = TableNames(schema=schema, prefix="x_")
    await migrate(client, names)
    async with client.connection() as connection:
        cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=names.table("documents")))
        assert (await cursor.fetchone()) == (0,)


async def test_documents_metadata_defaults_to_an_empty_object(client: _Client, names: TableNames) -> None:
    await migrate(client, names)
    async with client.connection() as connection:
        cursor = await connection.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = %s AND column_name = 'metadata'",
            (names.schema, "af_documents"),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert str(row[0]) == "'{}'::jsonb"


async def test_concurrent_first_runs_apply_once_and_never_raise(test_dsn: str, schema: str) -> None:
    for round_number in range(5):
        names = TableNames(schema=schema, prefix=f"r{round_number}_")
        clients = [_Client(SecretString(test_dsn), None) for _ in range(4)]
        try:
            reports = await asyncio.gather(*(migrate(c, names) for c in clients))
        finally:
            for c in clients:
                await c.close()
        assert sorted(r.applied for r in reports) == [(), (), (), (1,)]
        assert all(r.current == 1 for r in reports)
