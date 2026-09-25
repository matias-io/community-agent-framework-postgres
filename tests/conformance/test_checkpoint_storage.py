import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_framework import InMemoryCheckpointStorage, WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException

from agent_framework_community_postgres._checkpoint_storage import PostgresCheckpointStorage
from agent_framework_community_postgres._client import ClientHandle, TableNames


def _checkpoint(ts: datetime, *, previous: str | None = None) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        workflow_name="wf",
        graph_signature_hash="sig",
        checkpoint_id=str(uuid.uuid4()),
        previous_checkpoint_id=previous,
        timestamp=ts.isoformat(),
        iteration_count=0,
    )


@pytest.fixture(params=["memory", "postgres"])
def storage(request: pytest.FixtureRequest) -> Any:
    # Synchronous so getfixturevalue can set up the async postgres fixtures outside a running loop.
    if request.param == "memory":
        return InMemoryCheckpointStorage()
    client: ClientHandle = request.getfixturevalue("client")
    names: TableNames = request.getfixturevalue("migrated")
    return PostgresCheckpointStorage(application_id="conformance", client=client.client, schema=names.schema)


async def test_get_latest_picks_the_newest_timestamp(storage: Any) -> None:
    base = datetime(2026, 9, 30, tzinfo=UTC)
    first = _checkpoint(base)
    second = _checkpoint(base + timedelta(seconds=1), previous=first.checkpoint_id)
    await storage.save(second)
    await storage.save(first)
    latest = await storage.get_latest(workflow_name="wf")
    assert latest is not None and latest.checkpoint_id == second.checkpoint_id
    # The protocol leaves listing order open (InMemoryCheckpointStorage lists in insertion order);
    # PostgreSQL's oldest-first order is asserted in the integration tests.
    assert sorted(await storage.list_checkpoint_ids(workflow_name="wf")) == sorted(
        [first.checkpoint_id, second.checkpoint_id]
    )


async def test_missing_load_raises_and_delete_reports(storage: Any) -> None:
    with pytest.raises(WorkflowCheckpointException):
        await storage.load("missing")
    assert await storage.delete("missing") is False
    saved = _checkpoint(datetime.now(UTC))
    await storage.save(saved)
    assert await storage.delete(saved.checkpoint_id) is True
