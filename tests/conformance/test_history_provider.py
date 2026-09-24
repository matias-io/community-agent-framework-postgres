"""The same behaviour over MAF's in-memory provider and ours."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agent_framework import InMemoryHistoryProvider, Message

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._history_provider import PostgresHistoryProvider

Getter = Callable[[str], Awaitable[list[Message]]]
Saver = Callable[[str, list[Message]], Awaitable[None]]


@pytest.fixture(params=["memory", "postgres"])
def history(request: pytest.FixtureRequest) -> tuple[Getter, Saver]:
    # Synchronous so getfixturevalue can set up the async postgres fixtures outside a running loop.
    if request.param == "memory":
        provider = InMemoryHistoryProvider()
        states: dict[str, dict[str, Any]] = {}

        async def get(session_id: str) -> list[Message]:
            return await provider.get_messages(session_id, state=states.setdefault(session_id, {}))

        async def save(session_id: str, messages: list[Message]) -> None:
            await provider.save_messages(session_id, messages, state=states.setdefault(session_id, {}))

        return get, save
    client: ClientHandle = request.getfixturevalue("client")
    names: TableNames = request.getfixturevalue("migrated")
    pg_provider = PostgresHistoryProvider(application_id="conformance", client=client.client, schema=names.schema)

    async def pg_get(session_id: str) -> list[Message]:
        return await pg_provider.get_messages(session_id)

    async def pg_save(session_id: str, messages: list[Message]) -> None:
        await pg_provider.save_messages(session_id, messages)

    return pg_get, pg_save


async def test_empty_session_is_empty(history: tuple[Getter, Saver]) -> None:
    get, _ = history
    assert await get("none") == []


async def test_round_trip_preserves_role_and_text(history: tuple[Getter, Saver]) -> None:
    get, save = history
    await save("s", [Message(role="user", contents=["hello"]), Message(role="assistant", contents=["hi"])])
    assert [(m.role, m.text) for m in await get("s")] == [("user", "hello"), ("assistant", "hi")]


async def test_full_transcript_replay_is_deduplicated(history: tuple[Getter, Saver]) -> None:
    get, save = history
    turn = [Message(role="user", contents=["q"]), Message(role="assistant", contents=["a"])]
    await save("s", turn)
    await save("s", [*turn, Message(role="user", contents=["q2"])])
    assert [m.text for m in await get("s")] == ["q", "a", "q2"]
