import asyncio
from datetime import timedelta

import pytest
from agent_framework import AgentSession, Message

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


async def test_retention_tombstones(client: ClientHandle, migrated: TableNames) -> None:
    store = PostgresSessionStore(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.set("k", AgentSession())
    await asyncio.sleep(1.5)
    assert (await store.purge()).counts == {"af_sessions": 1}
    assert await store.get("k") is None
    await store.set("k", AgentSession())
    assert await store.get("k") is not None
