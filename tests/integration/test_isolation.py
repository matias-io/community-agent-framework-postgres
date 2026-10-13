"""Every write runs at READ COMMITTED, so a REPEATABLE READ server default never turns last-writer-wins into errors."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from agent_framework import AgentSession, WorkflowCheckpoint
from agent_framework_ag_ui import AGUIThreadSnapshot

from agent_framework_community_postgres import PostgresPersistence

pytestmark = pytest.mark.integration


@pytest.fixture
async def repeatable_read_hub(test_dsn: str, schema: str, monkeypatch: pytest.MonkeyPatch) -> PostgresPersistence:
    monkeypatch.setenv("PGOPTIONS", r"-c default_transaction_isolation=repeatable\ read")
    hub = PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema)
    await hub.open()
    await hub.migrate()
    async with hub._client.connection(transactional=False) as connection:  # noqa: SLF001
        cursor = await connection.execute("SHOW default_transaction_isolation")
        assert await cursor.fetchone() == ("repeatable read",)
    return hub


async def test_concurrent_session_writes_under_repeatable_read(repeatable_read_hub: PostgresPersistence) -> None:
    sessions = repeatable_read_hub.session_store()
    try:
        for round_number in range(5):
            key = f"s{round_number}"
            await asyncio.gather(*(sessions.set(key, AgentSession(session_id=key)) for _ in range(8)))
            await asyncio.gather(*(sessions.delete(key) for _ in range(4)))
    finally:
        await repeatable_read_hub.close()


async def test_concurrent_snapshot_writes_under_repeatable_read(repeatable_read_hub: PostgresPersistence) -> None:
    snapshots = repeatable_read_hub.thread_snapshot_store()
    snapshot = AGUIThreadSnapshot(messages=[], state=None, interrupt=None, session_state=None)
    try:
        for round_number in range(5):
            thread = f"t{round_number}"
            await asyncio.gather(*(snapshots.save(scope="u", thread_id=thread, snapshot=snapshot) for _ in range(8)))
            await asyncio.gather(*(snapshots.delete(scope="u", thread_id=thread) for _ in range(4)))
    finally:
        await repeatable_read_hub.close()


async def test_concurrent_resaves_of_one_checkpoint_under_repeatable_read(
    repeatable_read_hub: PostgresPersistence,
) -> None:
    # A save waiting on the scope's advisory lock must see the row the previous save committed.
    storage = repeatable_read_hub.checkpoint_storage(scope="c")
    try:
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
    finally:
        await repeatable_read_hub.close()
