import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import pytest
from agent_framework import Message
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._history_provider import purge_history
from agent_framework_community_postgres._retention import RetentionMode, RetentionPolicy, purge_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("mode", "tombstone"), [("delete", None), ("tombstone", sql.SQL("snapshot = NULL"))])
async def test_an_or_predicate_never_purges_live_rows(
    client: ClientHandle, migrated: TableNames, mode: RetentionMode, tombstone: sql.Composable | None
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
        mode=mode,
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


async def test_purge_keeps_an_active_session_whole(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider(retention=RetentionPolicy(ttl=timedelta(hours=1)))
    history = persistence.names.table("history_messages")
    await provider.save_messages(
        "s", [Message(role="user", contents=["old"]), Message(role="assistant", contents=["old-reply"])]
    )
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(
            sql.SQL("UPDATE {h} SET expires_at = now() - interval '1 minute' WHERE session_id = 's'").format(h=history)
        )
    await provider.save_messages("s", [Message(role="user", contents=["new"])])
    assert (await provider.purge()).total == 0
    assert (await persistence.purge()).total == 0
    assert [m.text for m in await provider.get_messages("s")] == ["old", "old-reply", "new"]
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(
            sql.SQL("UPDATE {h} SET expires_at = now() - interval '1 minute' WHERE session_id = 's'").format(h=history)
        )
    assert (await provider.purge()).total == 3


async def _expire_history(persistence: PostgresPersistence) -> None:
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(
            sql.SQL("UPDATE {h} SET expires_at = now() - interval '1 minute'").format(
                h=persistence.names.table("history_messages")
            )
        )


async def test_history_purge_takes_every_session_across_batches(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider(retention=RetentionPolicy(ttl=timedelta(hours=1)))
    for session in ("s1", "s2", "s3"):
        await provider.save_messages(
            session, [Message(role="user", contents=["q"]), Message(role="assistant", contents=["a"])]
        )
    await _expire_history(persistence)
    deleted = await purge_history(
        persistence._client,  # noqa: SLF001
        persistence.names,
        application_id=persistence.application_id,
        batch_size=1,
    )
    assert deleted == 6
    for session in ("s1", "s2", "s3"):
        assert await provider.get_messages(session) == []


async def test_history_purge_skips_a_session_whose_save_lock_is_held(
    persistence: PostgresPersistence, test_dsn: str
) -> None:
    provider = persistence.history_provider(retention=RetentionPolicy(ttl=timedelta(hours=1)))
    for session in ("held", "free"):
        await provider.save_messages(
            session, [Message(role="user", contents=["q"]), Message(role="assistant", contents=["a"])]
        )
    await _expire_history(persistence)
    async with await AsyncConnection.connect(test_dsn) as blocker:
        # The key a save of session "held" takes: schema, prefix, application, tenant, agent, source, session.
        await blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(concat_ws(chr(31), %s::text, 'af_', 'tests', '', '',"
            " 'postgres_history', 'held'), 0))",
            (persistence.names.schema,),
        )
        assert (await asyncio.wait_for(provider.purge(), 5)).total == 2
        assert [m.text for m in await provider.get_messages("held")] == ["q", "a"]
        await blocker.rollback()
    assert (await persistence.purge()).counts["af_history_messages"] == 2
    assert await provider.get_messages("held") == []


async def test_history_purge_keeps_a_session_with_a_live_message(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider(retention=RetentionPolicy(ttl=timedelta(hours=1)))
    await provider.save_messages("s", [Message(role="user", contents=["q"]), Message(role="assistant", contents=["a"])])
    await _expire_history(persistence)
    await provider.save_messages("s", [Message(role="user", contents=["again"])])
    assert (
        await purge_history(
            persistence._client,  # noqa: SLF001
            persistence.names,
            application_id=persistence.application_id,
            batch_size=1,
        )
        == 0
    )
    assert [m.text for m in await provider.get_messages("s")] == ["q", "a", "again"]


async def test_history_purge_rejects_a_batch_size_below_one(persistence: PostgresPersistence) -> None:
    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        await purge_history(
            persistence._client,  # noqa: SLF001
            persistence.names,
            application_id=persistence.application_id,
            batch_size=0,
        )


async def test_history_purge_locks_at_most_64_sessions_per_transaction(
    persistence: PostgresPersistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = persistence.history_provider(retention=RetentionPolicy(ttl=timedelta(hours=1)))
    for i in range(150):
        await provider.save_messages(f"s{i}", [Message(role="user", contents=["q"])])
    await _expire_history(persistence)
    handle = persistence._client  # noqa: SLF001
    original = handle.connection
    opened: list[int] = []

    def counting(**kwargs: Any) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        opened.append(1)
        return original(**kwargs)

    monkeypatch.setattr(handle, "connection", counting)
    assert await purge_history(handle, persistence.names, application_id=persistence.application_id) == 150
    assert len(opened) == 4  # 64 + 64 + 22 sessions, then a round that finds none
    assert await provider.list_sessions() == []
