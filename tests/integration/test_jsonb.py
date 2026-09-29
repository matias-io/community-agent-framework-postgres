import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from agent_framework import AgentSession, Message, WorkflowCheckpoint
from agent_framework_ag_ui import AGUIThreadSnapshot
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import PostgresPersistence

pytestmark = pytest.mark.integration

SECRET = "SECRETPAYLOAD"
TABLES = ("history_messages", "sessions", "checkpoints", "thread_snapshots", "documents")


def _writes(hub: PostgresPersistence, bad: object) -> dict[str, Callable[[], Awaitable[object]]]:
    session = AgentSession()
    session.state["bad"] = bad
    checkpoint = WorkflowCheckpoint(
        workflow_name="wf",
        graph_signature_hash="sig",
        checkpoint_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC).isoformat(),
        state={"bad": bad},
        iteration_count=0,
    )
    message = Message(role="user", contents=["x"], additional_properties={"bad": bad})
    return {
        "sessions": lambda: hub.session_store().set("s", session),
        "documents": lambda: hub.document_store(collection="c").put(scope="s", key="k", payload={"bad": bad}),
        "history": lambda: hub.history_provider().save_messages("s", [Message(role="user", contents=["ok"]), message]),
        "snapshots": lambda: hub.thread_snapshot_store().save(
            scope="s", thread_id="t", snapshot=AGUIThreadSnapshot(messages=[], state={"bad": bad})
        ),
        "checkpoints": lambda: hub.checkpoint_storage().save(checkpoint),
    }


@pytest.mark.parametrize("bad", [f"{SECRET}\x00", float("nan")])
@pytest.mark.parametrize("store", ["sessions", "documents", "history", "snapshots", "checkpoints"])
async def test_unstorable_json_is_refused_and_writes_nothing(
    persistence: PostgresPersistence, bad: object, store: str
) -> None:
    with pytest.raises(ValueError) as info:
        await _writes(persistence, bad)[store]()
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None
    pool = persistence.pool
    assert isinstance(pool, AsyncConnectionPool)
    async with pool.connection() as connection:
        for table in TABLES:
            cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {}").format(persistence.names.table(table)))
            assert await cursor.fetchone() == (0,), table
