import asyncio
from datetime import timedelta

import pytest
from agent_framework import Message

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._history_provider import PostgresHistoryProvider
from agent_framework_community_postgres._retention import RetentionPolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def history(client: ClientHandle, migrated: TableNames) -> PostgresHistoryProvider:
    return PostgresHistoryProvider(application_id="tests", client=client.client, schema=migrated.schema)


def _texts(messages: list[Message]) -> list[str | None]:
    return [m.text for m in messages]


async def test_save_and_get_keep_order(history: PostgresHistoryProvider) -> None:
    await history.save_messages("s1", [Message(role="user", contents=["a"]), Message(role="assistant", contents=["b"])])
    assert _texts(await history.get_messages("s1")) == ["a", "b"]
    assert await history.get_messages("s2") == []


async def test_replaying_the_full_transcript_does_not_duplicate(history: PostgresHistoryProvider) -> None:
    first = [Message(role="user", contents=["a"]), Message(role="assistant", contents=["b"])]
    await history.save_messages("s1", first)
    await history.save_messages("s1", [*first, Message(role="user", contents=["c"])])
    assert _texts(await history.get_messages("s1")) == ["a", "b", "c"]


async def test_scopes_isolate_history(client: ClientHandle, migrated: TableNames) -> None:
    kwargs = {"application_id": "tests", "client": client.client, "schema": migrated.schema}
    a = PostgresHistoryProvider(tenant_id="t1", **kwargs)
    b = PostgresHistoryProvider(tenant_id="t2", **kwargs)
    other_source = PostgresHistoryProvider("audit", tenant_id="t1", **kwargs)
    await a.save_messages("s", [Message(role="user", contents=["a"])])
    assert await b.get_messages("s") == []
    assert await other_source.get_messages("s") == []


async def test_max_messages_keeps_the_newest(client: ClientHandle, migrated: TableNames) -> None:
    history = PostgresHistoryProvider(
        application_id="tests", client=client.client, schema=migrated.schema, max_messages=2
    )
    await history.save_messages("s", [Message(role="user", contents=[t]) for t in ("a", "b", "c")])
    assert _texts(await history.get_messages("s")) == ["b", "c"]


async def test_max_messages_zero_stores_nothing(client: ClientHandle, migrated: TableNames) -> None:
    history = PostgresHistoryProvider(
        application_id="tests", client=client.client, schema=migrated.schema, max_messages=0
    )
    await history.save_messages("s", [Message(role="user", contents=["a"])])
    assert await history.get_messages("s") == []


async def test_clear_and_list_sessions(history: PostgresHistoryProvider) -> None:
    await history.save_messages("s1", [Message(role="user", contents=["a"])])
    await history.save_messages("s2", [Message(role="user", contents=["b"])])
    assert await history.list_sessions() == ["s1", "s2"]
    await history.clear("s1")
    assert await history.get_messages("s1") == []
    assert await history.list_sessions() == ["s2"]


async def test_retention_deletes_expired_messages(client: ClientHandle, migrated: TableNames) -> None:
    """History has no tombstone form: expired messages are deleted whatever the policy's mode."""
    history = PostgresHistoryProvider(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1), mode="tombstone"),
    )
    await history.save_messages("s", [Message(role="user", contents=["a"])])
    await asyncio.sleep(1.5)
    assert (await history.purge()).counts == {"af_history_messages": 1}
    assert await history.get_messages("s") == []
