from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from agent_framework import AgentSession
from psycopg import sql

from agent_framework_community_postgres import PostgresPersistence, RetentionPolicy
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


async def test_purge_runs_in_batches(
    persistence: PostgresPersistence, expire: Callable[[str], Awaitable[None]]
) -> None:
    store = persistence.session_store(retention=RetentionPolicy(ttl=timedelta(seconds=1), mode="delete"))
    for i in range(2500):
        await store.set(f"s{i}", AgentSession(session_id=f"s{i}"))
    await expire("sessions")
    assert (await store.purge()).counts == {"af_sessions": 2500}
    assert await store.get("s0") is None
