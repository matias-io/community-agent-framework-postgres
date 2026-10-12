import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from agent_framework import Message
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import PostgresPersistence, _framework
from agent_framework_community_postgres._client import ClientHandle, PostgresStorageError, TableNames
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


async def test_concurrent_saves_to_one_session_do_not_duplicate(history: PostgresHistoryProvider) -> None:
    turn = [Message(role="user", contents=["q"]), Message(role="assistant", contents=["a"])]
    await asyncio.gather(*(history.save_messages("race", turn) for _ in range(4)))
    assert _texts(await history.get_messages("race")) == ["q", "a"]


async def test_undecodable_rows_are_skipped(
    history: PostgresHistoryProvider, client: ClientHandle, migrated: TableNames
) -> None:
    await history.save_messages("s", [Message(role="user", contents=["good"])])
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL(
                "INSERT INTO {history} (application_id, tenant_id, agent_id, source_id, session_id, message)"
                " VALUES (%s, '', '', %s, %s, %s)"
            ).format(history=migrated.table("history_messages")),
            ["tests", history.source_id, "s", Jsonb({"type": "message", "role": "user", "contents": 5})],
        )
    assert _texts(await history.get_messages("s")) == ["good"]


async def test_a_repeated_input_is_kept_not_dropped_as_a_replay(history: PostgresHistoryProvider) -> None:
    # The MAF 1.20 rule: a repeated id-less input is a new message. 1.19 dropped it, which is why 1.20 is the floor.
    def stored() -> list[Message]:
        return [Message(role="user", contents=["good"])]

    def incoming() -> list[Message]:
        return [Message(role="user", contents=["good"]), Message(role="user", contents=["next"])]

    assert _texts(_framework.filter_new_messages(stored(), incoming())) == ["good", "next"]
    await history.save_messages("s", stored())
    await history.save_messages("s", incoming())
    assert _texts(await history.get_messages("s")) == ["good", "good", "next"]


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


async def test_retention_deletes_expired_messages(
    client: ClientHandle, migrated: TableNames, expire: Callable[[str], Awaitable[None]]
) -> None:
    """History has no tombstone form: expired messages are deleted whatever the policy's mode."""
    history = PostgresHistoryProvider(
        application_id="tests",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1), mode="tombstone"),
    )
    await history.save_messages("s", [Message(role="user", contents=["a"])])
    await expire("history_messages")
    assert (await history.purge()).counts == {"af_history_messages": 1}
    assert await history.get_messages("s") == []


async def test_concurrent_saves_under_repeatable_read(test_dsn: str, schema: str) -> None:
    async def repeatable_read(connection: AsyncConnection[Any]) -> None:
        await connection.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ")

    pool = AsyncConnectionPool(test_dsn, open=False, kwargs={"autocommit": True}, configure=repeatable_read)
    async with pool, PostgresPersistence(application_id="tests", client=pool, schema=schema) as hub:
        await hub.migrate()
        provider = hub.history_provider()
        transcript = [Message(role="user", contents=["hi"]), Message(role="assistant", contents=["hello"])]
        await asyncio.gather(*(provider.save_messages("s", transcript) for _ in range(6)))
        assert [m.text for m in await provider.get_messages("s")] == ["hi", "hello"]


async def test_save_reads_only_a_tail(persistence: PostgresPersistence, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = persistence.history_provider()
    await provider.save_messages("s", [Message(role="user", contents=[str(i)]) for i in range(50)])
    limits: list[int] = []
    original = provider._tail  # noqa: SLF001

    async def counting(connection: AsyncConnection[Any], params: dict[str, Any], limit: int) -> list[Message]:
        limits.append(limit)
        return await original(connection, params, limit)

    monkeypatch.setattr(provider, "_tail", counting)
    await provider.save_messages(
        "s", [Message(role="user", contents=["50"]), Message(role="assistant", contents=["51"])]
    )
    assert limits == [2]
    assert len(await provider.get_messages("s")) == 52


async def test_undecodable_row_in_the_window_fails_the_save(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider()
    await provider.save_messages("s", [Message(role="user", contents=["a"])])
    async with persistence._client.connection() as connection:  # noqa: SLF001
        await connection.execute(
            sql.SQL("UPDATE {h} SET message = '[]' WHERE session_id = 's'").format(
                h=persistence.names.table("history_messages")
            )
        )
    with pytest.raises(PostgresStorageError):
        await provider.save_messages("s", [Message(role="user", contents=["b"])])


async def test_clear_is_serialized_with_saves(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider()
    big = [Message(role="user", contents=[str(i)]) for i in range(200)]
    results = await asyncio.gather(provider.save_messages("s", big), provider.clear("s"), return_exceptions=True)
    assert all(r is None for r in results)
    assert len(await provider.get_messages("s")) in (0, 200)


async def test_list_sessions_pages(persistence: PostgresPersistence) -> None:
    provider = persistence.history_provider()
    for name in ("a", "b", "c"):
        await provider.save_messages(name, [Message(role="user", contents=["x"])])
    assert await provider.list_sessions(limit=2) == ["a", "b"]
    assert await provider.list_sessions(limit=2, after="b") == ["c"]
