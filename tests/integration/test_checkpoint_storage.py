import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import Executor, WorkflowBuilder, WorkflowCheckpoint, WorkflowContext, handler
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql
from psycopg.types.json import Jsonb

from agent_framework_community_postgres._checkpoint_storage import PostgresCheckpointStorage
from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import RetentionPolicy

pytestmark = pytest.mark.integration


def _checkpoint(
    workflow_name: str = "wf",
    *,
    checkpoint_id: str | None = None,
    previous: str | None = None,
    timestamp: str | None = None,
    state: dict | None = None,
) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        workflow_name=workflow_name,
        graph_signature_hash="sig",
        checkpoint_id=checkpoint_id or str(uuid.uuid4()),
        previous_checkpoint_id=previous,
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        state=state or {},
        iteration_count=0,
    )


@pytest.fixture
def storage(client: ClientHandle, migrated: TableNames) -> PostgresCheckpointStorage:
    return PostgresCheckpointStorage(application_id="tests", client=client.client, schema=migrated.schema)


async def test_save_load_round_trip_with_non_json_state(storage: PostgresCheckpointStorage) -> None:
    when = datetime(2026, 9, 30, 12, tzinfo=UTC)
    saved = _checkpoint(state={"when": when, "id": uuid.uuid4(), "n": 1})
    assert await storage.save(saved) == saved.checkpoint_id
    loaded = await storage.load(saved.checkpoint_id)
    assert loaded.checkpoint_id == saved.checkpoint_id
    assert loaded.state == saved.state
    assert loaded.workflow_name == "wf"


async def test_load_missing_raises(storage: PostgresCheckpointStorage) -> None:
    with pytest.raises(WorkflowCheckpointException):
        await storage.load("missing")


async def test_listing_orders_by_timestamp_and_latest_wins(storage: PostgresCheckpointStorage) -> None:
    base = datetime(2026, 9, 30, 12, tzinfo=UTC)
    older = _checkpoint(timestamp=(base - timedelta(minutes=1)).isoformat())
    newer = _checkpoint(timestamp=base.isoformat(), previous=older.checkpoint_id)
    await storage.save(newer)
    await storage.save(older)
    assert [c.checkpoint_id for c in await storage.list_checkpoints(workflow_name="wf")] == [
        older.checkpoint_id,
        newer.checkpoint_id,
    ]
    assert await storage.list_checkpoint_ids(workflow_name="wf") == [older.checkpoint_id, newer.checkpoint_id]
    latest = await storage.get_latest(workflow_name="wf")
    assert latest is not None and latest.checkpoint_id == newer.checkpoint_id
    assert await storage.get_latest(workflow_name="other") is None
    assert await storage.list_checkpoints(workflow_name="other") == []


async def test_delete(storage: PostgresCheckpointStorage) -> None:
    saved = _checkpoint()
    await storage.save(saved)
    assert await storage.delete(saved.checkpoint_id) is True
    assert await storage.delete(saved.checkpoint_id) is False


async def test_scope_separates_conversations(client: ClientHandle, migrated: TableNames) -> None:
    a = PostgresCheckpointStorage(application_id="tests", client=client.client, schema=migrated.schema, scope="conv-a")
    b = PostgresCheckpointStorage(application_id="tests", client=client.client, schema=migrated.schema, scope="conv-b")
    shared_id = "same-id"
    await a.save(_checkpoint(checkpoint_id=shared_id, state={"who": "a"}))
    await b.save(_checkpoint(checkpoint_id=shared_id, state={"who": "b"}))
    assert (await a.load(shared_id)).state == {"who": "a"}
    assert (await b.load(shared_id)).state == {"who": "b"}
    assert await b.get_latest(workflow_name="wf") is not None
    await a.delete(shared_id)
    assert (await b.load(shared_id)).state == {"who": "b"}


async def test_naive_timestamp_is_treated_as_utc_and_bad_timestamp_refused(storage: PostgresCheckpointStorage) -> None:
    naive = _checkpoint(timestamp="2026-09-30T12:00:00")
    await storage.save(naive)
    assert (await storage.load(naive.checkpoint_id)).timestamp == "2026-09-30T12:00:00"
    with pytest.raises(WorkflowCheckpointException):
        await storage.save(_checkpoint(timestamp="not a time"))


@dataclass
class _AppState:
    value: int


async def test_unregistered_type_is_refused_at_save_and_allowed_when_listed(
    client: ClientHandle, migrated: TableNames
) -> None:
    strict = PostgresCheckpointStorage(application_id="tests", client=client.client, schema=migrated.schema)
    with pytest.raises(WorkflowCheckpointException):
        await strict.save(_checkpoint(state={"app": _AppState(1)}))
    permissive = PostgresCheckpointStorage(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        allowed_checkpoint_types=[f"{_AppState.__module__}:{_AppState.__qualname__}"],
    )
    saved = _checkpoint(state={"app": _AppState(1)})
    await permissive.save(saved)
    assert (await permissive.load(saved.checkpoint_id)).state["app"] == _AppState(1)


async def test_retention_deletes_expired_checkpoints(client: ClientHandle, migrated: TableNames) -> None:
    storage = PostgresCheckpointStorage(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await storage.save(_checkpoint())
    await asyncio.sleep(1.5)
    assert (await storage.purge()).counts == {"af_checkpoints": 1}
    assert await storage.list_checkpoints(workflow_name="wf") == []


async def test_undecodable_rows_are_skipped_by_latest_and_ids(
    storage: PostgresCheckpointStorage, client: ClientHandle, migrated: TableNames, caplog: pytest.LogCaptureFixture
) -> None:
    base = datetime(2026, 9, 30, 12, tzinfo=UTC)
    good = _checkpoint(workflow_name="secret-workflow", timestamp=(base - timedelta(minutes=1)).isoformat())
    await storage.save(good)
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL(
                "INSERT INTO {} (application_id, scope, workflow_name, checkpoint_id, checkpoint_timestamp,"
                " iteration_count, encoded) VALUES ('tests', '', 'secret-workflow', 'secret-bad-id', %s, 0, %s)"
            ).format(migrated.table("checkpoints")),
            (base, Jsonb({"not": "a checkpoint"})),
        )
    with caplog.at_level(logging.WARNING):
        latest = await storage.get_latest(workflow_name="secret-workflow")
        ids = await storage.list_checkpoint_ids(workflow_name="secret-workflow")
    assert latest is not None and latest.checkpoint_id == good.checkpoint_id
    assert ids == [good.checkpoint_id]
    assert [c.checkpoint_id for c in await storage.list_checkpoints(workflow_name="secret-workflow")] == ids
    assert caplog.records
    assert all("secret" not in record.getMessage() for record in caplog.records)


class _Upper(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str]) -> None:
        await ctx.send_message(text.upper())


class _Finish(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str, str]) -> None:
        await ctx.yield_output(text + "!")


async def test_a_real_workflow_checkpoints_and_resumes(storage: PostgresCheckpointStorage) -> None:
    upper, finish = _Upper(id="upper"), _Finish(id="finish")
    workflow = WorkflowBuilder(start_executor=upper, checkpoint_storage=storage).add_edge(upper, finish).build()
    outputs = [event async for event in workflow.run("hello", stream=True)]
    assert any(getattr(event, "data", None) == "HELLO!" for event in outputs)
    checkpoints = await storage.list_checkpoints(workflow_name=workflow.name)
    assert checkpoints, "the run should have produced checkpoints"
    resumed = [event async for event in workflow.run(checkpoint_id=checkpoints[-1].checkpoint_id, stream=True)]
    assert resumed  # resuming from the last checkpoint runs without raising


async def test_checkpoint_messages_and_logs_name_no_ids(
    storage: PostgresCheckpointStorage, caplog: pytest.LogCaptureFixture
) -> None:
    checkpoint = _checkpoint(workflow_name="secret-workflow", checkpoint_id="secret-id")
    with caplog.at_level(logging.DEBUG, logger="agent_framework_community_postgres"):
        await storage.save(checkpoint)
    assert all("secret" not in record.getMessage() for record in caplog.records)
    with pytest.raises(WorkflowCheckpointException) as missing:
        await storage.load("secret-missing")
    assert "secret" not in str(missing.value)
    with pytest.raises(WorkflowCheckpointException) as bad_time:
        await storage.save(_checkpoint(checkpoint_id="secret-id-2", timestamp="not a time"))
    assert "secret" not in str(bad_time.value)
    unregistered = _checkpoint(checkpoint_id="secret-id-3", state={"app": _AppState(1)})
    with pytest.raises(WorkflowCheckpointException) as refused:
        await storage.save(unregistered)
    assert "secret" not in str(refused.value)
