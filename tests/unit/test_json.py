import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_framework import AgentSession, Message, WorkflowCheckpoint
from agent_framework_ag_ui import AGUIThreadSnapshot

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._json import encode_jsonb

SECRET = "SECRETPAYLOAD"
BAD_VALUES = [f"{SECRET}\x00", float("nan"), float("inf"), f"{SECRET}\ud800"]


def test_encodes_once_and_passes_the_text_through() -> None:
    value = {"a": [1, 2.5, "é", None]}
    adapted = encode_jsonb(value, what="Value")
    assert adapted.obj is value
    assert adapted.dumps(value) == json.dumps(value, ensure_ascii=False)


def test_a_literal_backslash_u0000_is_not_a_nul() -> None:
    encode_jsonb({"text": r"\u0000", "path": r"C:\\u0000"}, what="Value")


@pytest.mark.parametrize("value", [{"k": f"{SECRET}\x00"}, {f"{SECRET}\x00": 1}, [f"\\{SECRET}\x00", "\\\x00"]])
def test_nul_is_refused_without_quoting_the_value(value: object) -> None:
    with pytest.raises(ValueError) as info:
        encode_jsonb(value, what="Payload")
    assert str(info.value) == "Payload contains a NUL character, which PostgreSQL JSONB cannot store."
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("value", [{"k": [f"x{SECRET}\ud800y"]}, {f"{SECRET}\udfff": 1}])
def test_a_lone_surrogate_is_refused_without_quoting_the_value(value: object) -> None:
    with pytest.raises(ValueError) as info:
        encode_jsonb(value, what="Payload")
    assert str(info.value) == "Payload contains a lone surrogate, which is not valid UTF-8 and PostgreSQL cannot store."
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def _circular() -> dict[str, Any]:
    value: dict[str, Any] = {"secret": SECRET}
    value["self"] = value
    return value


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ({"x": float("nan"), "s": SECRET}, "ValueError"),
        ({"x": object(), "s": SECRET}, "TypeError"),
        (_circular(), "ValueError"),
    ],
)
def test_non_json_is_refused_with_the_error_type_only(value: object, kind: str) -> None:
    with pytest.raises(ValueError) as info:
        encode_jsonb(value, what="Payload")
    assert str(info.value) == f"Payload is not JSON-serializable ({kind})."
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.fixture
async def closed_hub() -> AsyncIterator[PostgresPersistence]:
    """Any SQL on this hub raises PostgresStorageError, so a ValueError proves none ran."""
    hub = PostgresPersistence(application_id="tests", connection_string="host=127.0.0.1 port=1")
    await hub.close()
    yield hub


def _writes(hub: PostgresPersistence, bad: object) -> list[Callable[[], Awaitable[object]]]:
    session = AgentSession()
    session.state["bad"] = bad
    documents = hub.document_store(collection="c")
    checkpoint = WorkflowCheckpoint(
        workflow_name="wf",
        graph_signature_hash="sig",
        checkpoint_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC).isoformat(),
        state={"bad": bad},
        iteration_count=0,
    )
    return [
        lambda: hub.session_store().set("s", session),
        lambda: documents.put(scope="s", key="k", payload={"bad": bad}),
        lambda: documents.put(scope="s", key="k", payload={}, metadata={"bad": bad}),
        lambda: (
            hub.history_provider().save_messages("s", [Message(role="user", contents=[str(bad)])])
            if isinstance(bad, str)
            else hub.history_provider().save_messages(
                "s", [Message(role="user", contents=["x"], additional_properties={"bad": bad})]
            )
        ),
        lambda: hub.thread_snapshot_store().save(
            scope="s", thread_id="t", snapshot=AGUIThreadSnapshot(messages=[], state={"bad": bad})
        ),
        lambda: hub.checkpoint_storage().save(checkpoint),
    ]


@pytest.mark.parametrize("bad", BAD_VALUES)
@pytest.mark.parametrize("index", range(6))
async def test_every_store_refuses_before_any_sql(closed_hub: PostgresPersistence, bad: object, index: int) -> None:
    with pytest.raises(ValueError) as info:
        await _writes(closed_hub, bad)[index]()
    assert SECRET not in str(info.value)
    assert any(reason in str(info.value) for reason in ("NUL", "not JSON-serializable", "lone surrogate"))
