import asyncio
from datetime import timedelta

import pytest
from agent_framework import AgentSession, Message

from agent_framework_community_postgres import PostgresPersistence, RetentionPolicy

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
    documents = persistence.document_store(collection="threads")
    await persistence.migrate()
    await documents.put(scope="u", key="t", payload={"n": 1})
    stored = await documents.get(scope="u", key="t")
    assert stored is not None
    assert stored.payload == {"n": 1}
    await persistence.close()
    assert persistence.pool.closed


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
