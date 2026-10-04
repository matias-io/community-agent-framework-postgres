import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

import pytest
from agent_framework import AgentSession, Message
from agent_framework_ag_ui import AGUIThreadSnapshot
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import PostgresPersistence, PostgresStorageError, RetentionPolicy

pytestmark = pytest.mark.integration


async def test_stores_share_the_pool_and_migrations_run_once(test_dsn: str, schema: str) -> None:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as persistence:
        report = await persistence.migrate()
        assert report.applied == (1,)
        assert await persistence.pending_migrations() == []
        history = persistence.history_provider()
        sessions = persistence.session_store()
        documents = persistence.document_store(collection="threads")
        assert history._client.client is persistence.pool  # noqa: SLF001
        assert sessions._client.client is persistence.pool  # noqa: SLF001
        await history.save_messages("s", [Message(role="user", contents=["a"])])
        await sessions.set("s", AgentSession())
        await documents.put(scope="u", key="t", payload={})
        assert (await history.get_messages("s"))[0].text == "a"
        await history.close()  # borrowed: must not close the shared pool
        assert await sessions.get("s") is not None
    assert persistence.pool.closed


async def test_stores_work_before_explicit_open(test_dsn: str, schema: str) -> None:
    persistence = PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema)
    try:
        documents = persistence.document_store(collection="threads")
        await persistence.migrate()
        await documents.put(scope="u", key="t", payload={"n": 1})
        stored = await documents.get(scope="u", key="t")
        assert stored is not None
        assert stored.payload == {"n": 1}
    finally:
        await persistence.close()
    assert persistence.pool.closed


async def test_closing_a_never_opened_hub_stops_its_stores(test_dsn: str, schema: str) -> None:
    persistence = PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema)
    pool = persistence.pool
    assert isinstance(pool, AsyncConnectionPool)
    try:
        sessions = persistence.session_store()
        documents = persistence.document_store(collection="threads")
        await persistence.close()
        with pytest.raises(PostgresStorageError):
            await sessions.get("x")
        with pytest.raises(PostgresStorageError):
            async with documents.lease(scope="s", key="k", owner="o", ttl=timedelta(seconds=30)):
                pass
        assert pool.closed
    finally:
        await pool.close()


@pytest.mark.parametrize("key", ["application_id", "client", "credential", "schema", "table_prefix"])
@pytest.mark.parametrize(
    "factory", ["history_provider", "session_store", "checkpoint_storage", "thread_snapshot_store", "document_store"]
)
async def test_factories_reject_overriding_hub_owned_arguments(key: str, factory: str) -> None:
    persistence = PostgresPersistence(application_id="tests", connection_string="host=h")
    arguments = {key: "other"}
    if factory == "document_store":
        arguments["collection"] = "threads"
    try:
        with pytest.raises(TypeError, match=f"{key} is set by the hub and cannot be overridden"):
            getattr(persistence, factory)(**arguments)
    finally:
        await persistence.close()


async def test_checkpoint_storage_forwards_scope_and_allowed_types(test_dsn: str, schema: str) -> None:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as persistence:
        storage = persistence.checkpoint_storage(scope="conv-1", allowed_checkpoint_types=["m:T"])
        assert storage.scope == "conv-1"
        assert storage._allowed_types == frozenset({"m:T"})  # noqa: SLF001
        assert storage._client.client is persistence.pool  # noqa: SLF001


async def test_thread_snapshots_and_leases_use_the_shared_pool(test_dsn: str, schema: str) -> None:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as persistence:
        await persistence.migrate()
        snapshots = persistence.thread_snapshot_store()
        snapshot = AGUIThreadSnapshot(messages=[{"id": "m1", "role": "user", "content": "hi"}], state={"n": 1})
        await snapshots.save(scope="u", thread_id="t", snapshot=snapshot)
        assert await snapshots.get(scope="u", thread_id="t") == snapshot
        leases = persistence.leases()
        assert leases._client.client is persistence.pool  # noqa: SLF001
        lease = await leases.try_acquire("r", owner="o", ttl=timedelta(seconds=30))
        assert lease is not None
        await lease.release()


async def test_hub_over_a_caller_pool_leaves_it_open(test_dsn: str, schema: str) -> None:
    pool: AsyncConnectionPool = AsyncConnectionPool(test_dsn, open=False, kwargs={"autocommit": True})
    await pool.open()
    try:
        async with PostgresPersistence(application_id="tests", client=pool, schema=schema) as persistence:
            assert persistence.pool is pool
            await persistence.migrate()
            await persistence.document_store(collection="threads").put(scope="u", key="t", payload={})
        assert not pool.closed
        async with pool.connection() as connection:
            await connection.execute("SELECT 1")
    finally:
        await pool.close()


async def test_purge_covers_every_store(test_dsn: str, schema: str) -> None:
    policy = RetentionPolicy(ttl=timedelta(seconds=1))
    async with PostgresPersistence(
        application_id="tests", connection_string=test_dsn, schema=schema, retention=policy
    ) as persistence:
        await persistence.migrate()
        await persistence.history_provider().save_messages("s", [Message(role="user", contents=["a"])])
        await persistence.session_store().set("s", AgentSession())
        await persistence.document_store(collection="threads").put(scope="u", key="t", payload={})
        await asyncio.sleep(1.5)
        report = await persistence.purge()
        assert report.counts == {
            "af_history_messages": 1,
            "af_sessions": 1,
            "af_documents": 1,
            "af_checkpoints": 0,
            "af_thread_snapshots": 0,
        }


async def test_hub_without_a_policy_purges_rows_a_store_policy_stamped(test_dsn: str, schema: str) -> None:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as persistence:
        await persistence.migrate()
        short = RetentionPolicy(ttl=timedelta(seconds=1))
        notes = persistence.document_store(collection="notes", retention=short)
        await notes.put(scope="u", key="old", payload={"v": 1})
        await persistence.document_store(collection="notes").put(scope="u", key="kept", payload={"v": 1})
        await persistence.session_store(retention=short).set("s", AgentSession())
        await asyncio.sleep(1.5)
        report = await persistence.purge()
        assert report.counts["af_documents"] == 1
        assert report.counts["af_sessions"] == 1
        assert await notes.get(scope="u", key="old") is None
        assert await notes.get(scope="u", key="kept") is not None
        assert [s.key for s in await notes.list(scope="u", include_purged=True)] == ["kept", "old"]


async def test_hub_purge_mode_argument_wins(test_dsn: str, schema: str) -> None:
    async with PostgresPersistence(application_id="tests", connection_string=test_dsn, schema=schema) as persistence:
        await persistence.migrate()
        notes = persistence.document_store(collection="notes", retention=RetentionPolicy(ttl=timedelta(seconds=1)))
        await notes.put(scope="u", key="old", payload={"v": 1})
        await asyncio.sleep(1.5)
        assert (await persistence.purge(mode="delete")).counts["af_documents"] == 1
        assert await notes.list(scope="u", include_purged=True) == []


async def test_hub_purge_commits_each_table_on_its_own(
    persistence: PostgresPersistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = persistence._client  # noqa: SLF001
    original = handle.connection
    opened: list[int] = []

    def counting() -> AbstractAsyncContextManager[AsyncConnection]:
        opened.append(1)
        return original()

    monkeypatch.setattr(handle, "connection", counting)
    report = await persistence.purge()
    assert len(report.counts) == 5
    assert len(opened) == 5
