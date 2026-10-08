import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import purge_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("mode", "tombstone"), [("delete", None), ("tombstone", sql.SQL("snapshot = NULL"))])
async def test_an_or_predicate_never_purges_live_rows(
    client: ClientHandle, migrated: TableNames, mode: str, tombstone: sql.Composable | None
) -> None:
    sessions = migrated.table("sessions")
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL(
                "INSERT INTO {} (application_id, session_id, snapshot, snapshot_version, expires_at)"
                " VALUES ('tests', 'live', '{{}}', '1', now() + interval '1 hour'),"
                " ('tests', 'expired', '{{}}', '1', now() - interval '1 second')"
            ).format(sessions)
        )
    count = await purge_rows(
        client,
        table=sessions,
        where=sql.SQL("session_id = %(a)s OR session_id = %(b)s"),
        params={"a": "live", "b": "expired"},
        mode=mode,  # type: ignore[arg-type]
        tombstone=tombstone,
    )
    assert count == 1
    async with client.connection() as connection:
        cursor = await connection.execute(
            sql.SQL("SELECT session_id FROM {} WHERE snapshot IS NOT NULL ORDER BY session_id").format(sessions)
        )
        assert await cursor.fetchall() == [("live",)]


async def _insert_expired_sessions(persistence: PostgresPersistence, count: int) -> None:
    async with persistence._client.connection() as connection, connection.cursor() as cursor:  # noqa: SLF001
        await cursor.executemany(
            sql.SQL(
                "INSERT INTO {} (application_id, session_id, snapshot, snapshot_version, expires_at)"
                " VALUES (%s, %s, '{{}}', '1', now() - interval '1 minute')"
            ).format(persistence.names.table("sessions")),
            [(persistence.application_id, f"s{i}") for i in range(count)],
        )


async def test_purge_runs_one_transaction_per_batch(
    persistence: PostgresPersistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_expired_sessions(persistence, 25)
    handle = persistence._client  # noqa: SLF001
    original = handle.connection
    opened: list[int] = []

    def counting(**kwargs: Any) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        opened.append(1)
        return original(**kwargs)

    monkeypatch.setattr(handle, "connection", counting)
    count = await purge_rows(
        handle,
        table=persistence.names.table("sessions"),
        where=sql.SQL("application_id = %(app)s"),
        params={"app": persistence.application_id},
        mode="delete",
        tombstone=None,
        batch_size=10,
    )
    assert count == 25
    assert len(opened) == 3  # 10 + 10 + 5


async def test_purge_skips_a_row_another_transaction_holds(persistence: PostgresPersistence, test_dsn: str) -> None:
    await _insert_expired_sessions(persistence, 5)
    sessions = persistence.names.table("sessions")

    async def purge() -> int:
        return await purge_rows(
            persistence._client,  # noqa: SLF001
            table=sessions,
            where=sql.SQL("application_id = %(app)s"),
            params={"app": persistence.application_id},
            mode="delete",
            tombstone=None,
        )

    async with await AsyncConnection.connect(test_dsn) as blocker:
        await blocker.execute(
            sql.SQL("SELECT 1 FROM {} WHERE application_id = %(app)s AND session_id = 's0' FOR UPDATE").format(
                sessions
            ),
            {"app": persistence.application_id},
        )
        assert await asyncio.wait_for(purge(), 5) == 4
        await blocker.rollback()
    assert await purge() == 1


async def test_batch_size_below_one_is_rejected(client: ClientHandle, migrated: TableNames) -> None:
    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        await purge_rows(
            client,
            table=migrated.table("sessions"),
            where=sql.SQL("true"),
            params={},
            mode="delete",
            tombstone=None,
            batch_size=0,
        )
