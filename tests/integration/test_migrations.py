import asyncio
import time
from datetime import timedelta

import pytest
from agent_framework import SecretString
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import _migrations
from agent_framework_community_postgres._client import ClientHandle, PostgresStorageError, TableNames
from agent_framework_community_postgres._migrations import current_version, migrate, pending_versions

pytestmark = pytest.mark.integration


async def test_migrate_is_idempotent_and_records_versions(client: ClientHandle, names: TableNames) -> None:
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
            "leases",
        )
    )


async def test_prefix_and_schema_are_honoured(client: ClientHandle, schema: str) -> None:
    names = TableNames(schema=schema, prefix="x_")
    await migrate(client, names)
    async with client.connection() as connection:
        cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=names.table("sessions")))
        assert (await cursor.fetchone()) == (0,)


async def test_concurrent_first_runs_apply_once_and_never_raise(test_dsn: str, schema: str) -> None:
    for round_number in range(5):
        names = TableNames(schema=schema, prefix=f"r{round_number}_")
        clients = [ClientHandle(SecretString(test_dsn), None) for _ in range(4)]
        try:
            reports = await asyncio.gather(*(migrate(c, names) for c in clients))
        finally:
            for c in clients:
                await c.close()
        assert sorted(r.applied for r in reports) == [(), (), (), (1,)]
        assert all(r.current == 1 for r in reports)


async def test_concurrent_first_runs_under_repeatable_read(
    test_dsn: str, schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A waiting replica's snapshot would predate the version another replica committed; it would rerun v1.
    monkeypatch.setenv("PGOPTIONS", r"-c default_transaction_isolation=repeatable\ read")
    for round_number in range(5):
        names = TableNames(schema=schema, prefix=f"rr{round_number}_")
        clients = [ClientHandle(SecretString(test_dsn), None) for _ in range(4)]
        try:
            reports = await asyncio.gather(*(migrate(c, names) for c in clients))
        finally:
            for c in clients:
                await c.close()
        assert sorted(r.applied for r in reports) == [(), (), (), (1,)]


_NEWER = "Database schema version 99 is newer than this package supports (1); upgrade the package."


async def test_a_database_newer_than_the_package_is_refused(client: ClientHandle, migrated: TableNames) -> None:
    async with client.connection() as connection:
        await connection.execute(sql.SQL("INSERT INTO {} (version) VALUES (99)").format(migrated.table("migrations")))
    assert await current_version(client, migrated) == 99
    with pytest.raises(PostgresStorageError) as info:
        await pending_versions(client, migrated)
    assert str(info.value) == _NEWER
    with pytest.raises(PostgresStorageError) as info:
        await migrate(client, migrated)
    assert str(info.value) == _NEWER


@pytest.mark.timeout(30)
async def test_migration_ddl_runs_under_a_lock_timeout(
    client: ClientHandle, names: TableNames, test_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A later version's DDL that waits on a lock held by application traffic must fail with 55P03 within
    # lock_timeout instead of queueing forever (and blocking every query that queues behind it).
    await migrate(client, names)

    def fake_v2(names: TableNames) -> list[sql.Composed]:
        return [sql.SQL("ALTER TABLE {s} ADD COLUMN lock_probe int").format(s=names.table("sessions"))]

    monkeypatch.setattr(_migrations, "MIGRATIONS", (_migrations._v1, fake_v2))  # pyright: ignore[reportPrivateUsage]
    blocker = await AsyncConnection.connect(test_dsn)
    try:
        await blocker.execute(sql.SQL("LOCK TABLE {s} IN ACCESS EXCLUSIVE MODE").format(s=names.table("sessions")))
        started = time.monotonic()
        with pytest.raises(PostgresStorageError) as info:
            await migrate(client, names, lock_timeout=timedelta(seconds=1))
        assert time.monotonic() - started < 10
        assert info.value.sqlstate == "55P03"
        assert info.value.retryable is True
    finally:
        await blocker.rollback()
        await blocker.close()
    assert await current_version(client, names) == 1


async def test_migrating_inside_a_caller_transaction_keeps_its_lock_timeout(test_dsn: str, names: TableNames) -> None:
    async with await AsyncConnection.connect(test_dsn) as connection:
        await connection.execute("SET LOCAL lock_timeout = '7s'")
        report = await migrate(ClientHandle(None, connection), names)
        assert report.applied == (1,)
        cursor = await connection.execute("SELECT current_setting('lock_timeout')")
        assert await cursor.fetchone() == ("7s",)
        await connection.rollback()
