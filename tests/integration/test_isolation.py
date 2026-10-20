"""Every write runs at READ COMMITTED, so a REPEATABLE READ server default never turns last-writer-wins into errors."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_framework import AgentSession, WorkflowCheckpoint
from agent_framework_ag_ui import AGUIThreadSnapshot
from psycopg import AsyncConnection

from agent_framework_community_postgres import PostgresPersistence

pytestmark = pytest.mark.integration


@pytest.fixture
async def repeatable_read_hub(
    test_dsn: str, schema: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[PostgresPersistence]:
    monkeypatch.setenv("PGOPTIONS", r"-c default_transaction_isolation=repeatable\ read")
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as hub:
        await hub.migrate()
        async with hub._client.connection(transactional=False) as connection:  # noqa: SLF001
            cursor = await connection.execute("SHOW default_transaction_isolation")
            assert await cursor.fetchone() == ("repeatable read",)
        yield hub


async def test_concurrent_session_writes_under_repeatable_read(repeatable_read_hub: PostgresPersistence) -> None:
    sessions = repeatable_read_hub.session_store()
    for round_number in range(5):
        key = f"s{round_number}"
        await asyncio.gather(*(sessions.set(key, AgentSession(session_id=key)) for _ in range(8)))
        await asyncio.gather(*(sessions.delete(key) for _ in range(4)))


async def test_concurrent_snapshot_writes_under_repeatable_read(repeatable_read_hub: PostgresPersistence) -> None:
    snapshots = repeatable_read_hub.thread_snapshot_store()
    snapshot = AGUIThreadSnapshot(messages=[], state=None, interrupt=None, session_state=None)
    for round_number in range(5):
        thread = f"t{round_number}"
        await asyncio.gather(*(snapshots.save(scope="u", thread_id=thread, snapshot=snapshot) for _ in range(8)))
        await asyncio.gather(*(snapshots.delete(scope="u", thread_id=thread) for _ in range(4)))


async def test_concurrent_resaves_of_one_checkpoint_under_repeatable_read(
    repeatable_read_hub: PostgresPersistence,
) -> None:
    # A save waiting on the scope's advisory lock must see the row the previous save committed.
    storage = repeatable_read_hub.checkpoint_storage(scope="c")
    for _ in range(5):
        checkpoint = WorkflowCheckpoint(
            workflow_name="wf",
            graph_signature_hash="sig",
            checkpoint_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            state={},
            iteration_count=0,
        )
        await asyncio.gather(*(storage.save(checkpoint) for _ in range(8)))
        assert await storage.list_checkpoint_ids(workflow_name="wf")


async def test_every_purge_transaction_runs_at_read_committed(
    repeatable_read_hub: PostgresPersistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under a REPEATABLE READ default, SKIP LOCKED on a row another writer changed after the snapshot raises a
    # serialization failure, and a purge's liveness check would miss a save that committed meanwhile.
    handle = repeatable_read_hub._client  # noqa: SLF001
    original = handle.connection
    calls: list[dict[str, Any]] = []

    def recording(**kwargs: Any) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(handle, "connection", recording)
    report = await repeatable_read_hub.purge()
    assert report.total == 0
    assert calls
    assert all(call.get("read_committed") is True for call in calls), calls
