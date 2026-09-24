import pytest
from agent_framework import AgentSession, SessionStore

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._session_store import PostgresSessionStore


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> SessionStore:
    # Synchronous so getfixturevalue can set up the async postgres fixtures outside a running loop.
    if request.param == "memory":
        return SessionStore()
    client: ClientHandle = request.getfixturevalue("client")
    names: TableNames = request.getfixturevalue("migrated")
    return PostgresSessionStore(application_id="conformance", client=client.client, schema=names.schema)


async def test_get_missing_is_none(store: SessionStore) -> None:
    assert await store.get("missing") is None


async def test_set_then_get_is_a_copy(store: SessionStore) -> None:
    session = AgentSession(session_id="s")
    session.state["n"] = 1
    await store.set("k", session)
    loaded = await store.get("k")
    assert loaded is not None and loaded is not session and loaded.to_dict() == session.to_dict()


async def test_delete_is_idempotent(store: SessionStore) -> None:
    await store.set("k", AgentSession())
    await store.delete("k")
    await store.delete("k")
    assert await store.get("k") is None


async def test_empty_id_is_rejected(store: SessionStore) -> None:
    with pytest.raises(ValueError):
        await store.get("")
