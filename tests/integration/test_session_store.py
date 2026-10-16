import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from agent_framework import AgentSession, Message
from psycopg import sql

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import RetentionPolicy
from agent_framework_community_postgres._session_store import PostgresSessionStore

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions(client: ClientHandle, migrated: TableNames) -> PostgresSessionStore:
    return PostgresSessionStore(application_id="tests", client=client.client, schema=migrated.schema)


async def test_set_get_returns_an_independent_equal_session(sessions: PostgresSessionStore) -> None:
    session = AgentSession(session_id="abc", service_session_id="svc-1")
    session.state["messages"] = [Message(role="user", contents=["hi"])]
    session.state["counter"] = 3
    await sessions.set("key-1", session)
    loaded = await sessions.get("key-1")
    assert loaded is not None and loaded is not session
    assert loaded.session_id == "abc" and loaded.service_session_id == "svc-1"
    assert loaded.to_dict() == session.to_dict()
    loaded.state["counter"] = 4
    again = await sessions.get("key-1")
    assert again is not None and again.state["counter"] == 3


async def test_missing_and_deleted_sessions_are_none(sessions: PostgresSessionStore) -> None:
    assert await sessions.get("nope") is None
    await sessions.set("k", AgentSession())
    await sessions.delete("k")
    assert await sessions.get("k") is None
    await sessions.delete("k")  # idempotent


async def test_non_finite_floats_fail_before_any_write(sessions: PostgresSessionStore) -> None:
    session = AgentSession()
    session.state["bad"] = float("nan")
    with pytest.raises(ValueError):
        await sessions.set("k", session)
    assert await sessions.get("k") is None


async def test_invalid_ids_are_rejected(sessions: PostgresSessionStore) -> None:
    with pytest.raises(ValueError):
        await sessions.get("")
    with pytest.raises(ValueError):
        await sessions.set("", AgentSession())


async def test_retention_tombstones(
    client: ClientHandle, migrated: TableNames, expire: Callable[[str], Awaitable[None]]
) -> None:
    store = PostgresSessionStore(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.set("k", AgentSession())
    await expire("sessions")
    assert (await store.purge()).counts == {"af_sessions": 1}
    assert await store.get("k") is None
    await store.set("k", AgentSession())
    assert await store.get("k") is not None


async def test_corrupt_snapshot_is_loud(persistence: PostgresPersistence) -> None:
    store = persistence.session_store()
    await store.set("s", AgentSession(session_id="s"))
    sessions = persistence.names.table("sessions")
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(sql.SQL("UPDATE {t} SET snapshot = '[]' WHERE session_id = 's'").format(t=sessions))
    with pytest.raises(ValueError, match="not an object"):
        await store.get("s")
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(
            sql.SQL("UPDATE {t} SET snapshot = '{{}}', snapshot_version = '99' WHERE session_id = 's'").format(
                t=sessions
            )
        )
    with pytest.raises(ValueError, match="version"):
        await store.get("s")


async def test_the_last_write_wins(persistence: PostgresPersistence) -> None:
    store = persistence.session_store()
    first, second = AgentSession(session_id="s"), AgentSession(session_id="s")
    first.state["n"], second.state["n"] = 1, 2
    await store.set("s", first)
    await store.set("s", second)
    loaded = await store.get("s")
    assert loaded is not None and loaded.state["n"] == 2


async def test_concurrent_writers_both_succeed_and_one_snapshot_remains(persistence: PostgresPersistence) -> None:
    store = persistence.session_store()
    first, second = AgentSession(session_id="s"), AgentSession(session_id="s")
    first.state["n"], second.state["n"] = 1, 2
    await asyncio.gather(store.set("s", first), store.set("s", second))
    loaded = await store.get("s")
    assert loaded is not None and loaded.state["n"] in (1, 2)
