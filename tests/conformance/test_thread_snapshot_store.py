# tests/conformance/test_thread_snapshot_store.py
from typing import Any

import pytest
from agent_framework_ag_ui import AGUIThreadSnapshot, InMemoryAGUIThreadSnapshotStore

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._thread_snapshot_store import PostgresAGUIThreadSnapshotStore


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Any:
    # Synchronous so getfixturevalue can set up the async postgres fixtures outside a running loop.
    if request.param == "memory":
        return InMemoryAGUIThreadSnapshotStore()
    client: ClientHandle = request.getfixturevalue("client")
    names: TableNames = request.getfixturevalue("migrated")
    return PostgresAGUIThreadSnapshotStore(application_id="conformance", client=client.client, schema=names.schema)


async def test_missing_is_none_and_last_write_wins(store: Any) -> None:
    assert await store.get(scope="s", thread_id="t") is None
    await store.save(scope="s", thread_id="t", snapshot=AGUIThreadSnapshot(messages=[{"id": "1"}]))
    await store.save(scope="s", thread_id="t", snapshot=AGUIThreadSnapshot(messages=[{"id": "2"}], state={"k": 1}))
    loaded = await store.get(scope="s", thread_id="t")
    assert loaded == AGUIThreadSnapshot(messages=[{"id": "2"}], state={"k": 1})


async def test_returned_snapshot_is_a_copy(store: Any) -> None:
    await store.save(scope="s", thread_id="t", snapshot=AGUIThreadSnapshot(messages=[{"id": "1"}]))
    first = await store.get(scope="s", thread_id="t")
    first.messages.append({"id": "mutated"})
    second = await store.get(scope="s", thread_id="t")
    assert second.messages == [{"id": "1"}]


async def test_delete_reports_and_clear_scopes(store: Any) -> None:
    await store.save(scope="a", thread_id="t", snapshot=AGUIThreadSnapshot())
    await store.save(scope="b", thread_id="t", snapshot=AGUIThreadSnapshot())
    assert await store.delete(scope="a", thread_id="t") is True
    assert await store.delete(scope="a", thread_id="t") is False
    await store.clear(scope="b")
    assert await store.get(scope="b", thread_id="t") is None


async def test_empty_scope_is_rejected(store: Any) -> None:
    with pytest.raises(ValueError):
        await store.get(scope="", thread_id="t")
