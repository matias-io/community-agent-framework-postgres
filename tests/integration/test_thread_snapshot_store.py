# tests/integration/test_thread_snapshot_store.py
import asyncio
from datetime import timedelta

import pytest
from agent_framework_ag_ui import AGUIThreadSnapshot

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import RetentionPolicy
from agent_framework_community_postgres._thread_snapshot_store import PostgresAGUIThreadSnapshotStore

pytestmark = pytest.mark.integration


@pytest.fixture
def snapshots(client: ClientHandle, migrated: TableNames) -> PostgresAGUIThreadSnapshotStore:
    return PostgresAGUIThreadSnapshotStore(application_id="tests", client=client.client, schema=migrated.schema)


def _snapshot(n: int) -> AGUIThreadSnapshot:
    return AGUIThreadSnapshot(
        messages=[{"id": f"m{n}", "role": "user", "content": "hi"}],
        state={"n": n},
        interrupt=[{"id": "i1"}] if n == 1 else None,
        session_state={"messages": []},
    )


async def test_save_get_replaces_and_copies(snapshots: PostgresAGUIThreadSnapshotStore) -> None:
    await snapshots.save(scope="u1", thread_id="t1", snapshot=_snapshot(1))
    await snapshots.save(scope="u1", thread_id="t1", snapshot=_snapshot(2))
    loaded = await snapshots.get(scope="u1", thread_id="t1")
    assert loaded == _snapshot(2)
    assert await snapshots.get(scope="u2", thread_id="t1") is None


async def test_delete_and_clear(snapshots: PostgresAGUIThreadSnapshotStore) -> None:
    await snapshots.save(scope="u1", thread_id="t1", snapshot=_snapshot(1))
    await snapshots.save(scope="u1", thread_id="t2", snapshot=_snapshot(1))
    await snapshots.save(scope="u2", thread_id="t1", snapshot=_snapshot(1))
    assert await snapshots.delete(scope="u1", thread_id="t1") is True
    assert await snapshots.delete(scope="u1", thread_id="t1") is False
    await snapshots.clear(scope="u1")
    assert await snapshots.get(scope="u1", thread_id="t2") is None
    assert await snapshots.get(scope="u2", thread_id="t1") is not None
    await snapshots.clear()
    assert await snapshots.get(scope="u2", thread_id="t1") is None


async def test_empty_scope_or_thread_is_rejected(snapshots: PostgresAGUIThreadSnapshotStore) -> None:
    with pytest.raises(ValueError):
        await snapshots.get(scope="", thread_id="t")
    with pytest.raises(ValueError):
        await snapshots.save(scope="u", thread_id="", snapshot=_snapshot(1))


async def test_retention_tombstones(client: ClientHandle, migrated: TableNames) -> None:
    store = PostgresAGUIThreadSnapshotStore(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.save(scope="u", thread_id="t", snapshot=_snapshot(1))
    await asyncio.sleep(1.5)
    assert (await store.purge()).counts == {"af_thread_snapshots": 1}
    assert await store.get(scope="u", thread_id="t") is None
    assert await store.delete(scope="u", thread_id="t") is False  # a tombstone is not a live snapshot
    await store.save(scope="u", thread_id="t", snapshot=_snapshot(2))
    assert await store.delete(scope="u", thread_id="t") is True
    assert await store.delete(scope="u", thread_id="t") is False
