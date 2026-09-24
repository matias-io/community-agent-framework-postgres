import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import sql

from agent_framework_community_postgres._client import ClientHandle, LeaseUnavailable, RevisionConflict, TableNames
from agent_framework_community_postgres._document_store import PostgresDocumentStore
from agent_framework_community_postgres._retention import RetentionPolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def documents(client: ClientHandle, migrated: TableNames) -> PostgresDocumentStore:
    return PostgresDocumentStore(
        application_id="tests", collection="threads", client=client.client, schema=migrated.schema
    )


async def test_put_get_revisions(documents: PostgresDocumentStore) -> None:
    assert await documents.get(scope="anon:1", key="t1") is None
    assert await documents.put(scope="anon:1", key="t1", payload={"turn": 1}, metadata={"title": "a"}) == 1
    assert await documents.put(scope="anon:1", key="t1", payload={"turn": 2}) == 2
    doc = await documents.get(scope="anon:1", key="t1")
    assert doc is not None
    assert doc.payload == {"turn": 2}
    assert doc.metadata == {"title": "a"}  # None metadata leaves the stored metadata alone
    assert doc.revision == 2
    assert doc.updated_at >= doc.created_at
    assert doc.expires_at is None


async def test_scope_isolates_keys(documents: PostgresDocumentStore) -> None:
    await documents.put(scope="anon:1", key="t1", payload={"who": "one"})
    await documents.put(scope="anon:2", key="t1", payload={"who": "two"})
    one = await documents.get(scope="anon:1", key="t1")
    two = await documents.get(scope="anon:2", key="t1")
    assert one is not None and two is not None
    assert (one.payload["who"], two.payload["who"]) == ("one", "two")


async def test_expected_revision(documents: PostgresDocumentStore) -> None:
    with pytest.raises(RevisionConflict):
        await documents.put(scope="s", key="k", payload={}, expected_revision=1)  # nothing stored yet
    assert await documents.put(scope="s", key="k", payload={"v": 1}, expected_revision=0) == 1
    with pytest.raises(RevisionConflict):
        await documents.put(scope="s", key="k", payload={"v": 2}, expected_revision=0)  # already exists
    assert await documents.put(scope="s", key="k", payload={"v": 2}, expected_revision=1) == 2
    with pytest.raises(RevisionConflict):
        await documents.put(scope="s", key="k", payload={"v": 3}, expected_revision=1)  # stale


async def test_list_orders_newest_first_and_paginates(documents: PostgresDocumentStore) -> None:
    for key in ("a", "b", "c"):
        await documents.put(scope="s", key=key, payload={}, metadata={"title": key})
        await asyncio.sleep(0.01)
    summaries = await documents.list(scope="s", limit=2)
    assert [s.key for s in summaries] == ["c", "b"]
    older = await documents.list(scope="s", limit=2, before=summaries[-1])
    assert [s.key for s in older] == ["a"]
    assert older[0].metadata == {"title": "a"}


async def test_delete(documents: PostgresDocumentStore) -> None:
    await documents.put(scope="s", key="k", payload={})
    assert await documents.delete(scope="s", key="k") is True
    assert await documents.delete(scope="s", key="k") is False
    assert await documents.get(scope="s", key="k") is None


async def test_retention_tombstone(client: ClientHandle, migrated: TableNames) -> None:
    store = PostgresDocumentStore(
        application_id="tests",
        collection="threads",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.put(scope="s", key="old", payload={"x": 1}, metadata={"title": "old"})
    doc = await store.get(scope="s", key="old")
    assert doc is not None and doc.expires_at is not None
    assert (await store.purge()).total == 0
    await asyncio.sleep(1.5)
    report = await store.purge()
    assert report.counts == {"af_documents": 1}
    assert await store.get(scope="s", key="old") is None
    assert await store.list(scope="s") == []
    purged = await store.list(scope="s", include_purged=True)
    assert purged[0].purged_at is not None and purged[0].metadata == {"title": "old"}
    assert (await store.purge()).total == 0  # already tombstoned rows are not counted again


async def test_retention_delete(client: ClientHandle, migrated: TableNames) -> None:
    store = PostgresDocumentStore(
        application_id="tests",
        collection="threads",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1), mode="delete"),
    )
    await store.put(scope="s", key="old", payload={})
    await asyncio.sleep(1.5)
    assert (await store.purge()).counts == {"af_documents": 1}
    assert await store.list(scope="s", include_purged=True) == []


async def test_write_after_tombstone_revives_the_row(client: ClientHandle, migrated: TableNames) -> None:
    store = PostgresDocumentStore(
        application_id="tests",
        collection="threads",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.put(scope="s", key="k", payload={"v": 1})
    await asyncio.sleep(1.5)
    await store.purge()
    assert await store.put(scope="s", key="k", payload={"v": 2}) == 2
    doc = await store.get(scope="s", key="k")
    assert doc is not None and doc.payload == {"v": 2}


async def test_lease_on_a_document(documents: PostgresDocumentStore) -> None:
    async with documents.lease(scope="s", key="k", owner="one", ttl=timedelta(seconds=30)):
        with pytest.raises(LeaseUnavailable):
            async with documents.lease(scope="s", key="k", owner="two", ttl=timedelta(seconds=30)):
                pass


async def _tombstoned(client: ClientHandle, migrated: TableNames) -> tuple[PostgresDocumentStore, int]:
    store = PostgresDocumentStore(
        application_id="tests",
        collection="threads",
        client=client.client,
        schema=migrated.schema,
        retention=RetentionPolicy(ttl=timedelta(seconds=1)),
    )
    await store.put(scope="s", key="k", payload={"v": 1})
    revision = await store.put(scope="s", key="k", payload={"v": 2})
    await asyncio.sleep(1.5)
    assert (await store.purge()).total == 1
    assert await store.get(scope="s", key="k") is None
    return store, revision


async def test_expected_revision_zero_recreates_a_purged_document(client: ClientHandle, migrated: TableNames) -> None:
    store, old = await _tombstoned(client, migrated)
    assert await store.put(scope="s", key="k", payload={"v": 3}, expected_revision=0) == old + 1
    doc = await store.get(scope="s", key="k")
    assert doc is not None and doc.payload == {"v": 3}
    with pytest.raises(RevisionConflict):
        await store.put(scope="s", key="k", payload={"v": 4}, expected_revision=0)  # live now


async def test_expected_revision_n_revives_a_purged_document(client: ClientHandle, migrated: TableNames) -> None:
    store, old = await _tombstoned(client, migrated)
    assert await store.put(scope="s", key="k", payload={"v": 3}, expected_revision=old) == old + 1
    doc = await store.get(scope="s", key="k")
    assert doc is not None and doc.payload == {"v": 3}


async def test_list_pages_across_equal_updated_at(
    documents: PostgresDocumentStore, client: ClientHandle, migrated: TableNames
) -> None:
    for key in ("a", "b", "c"):
        await documents.put(scope="s", key=key, payload={})
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL("UPDATE {documents} SET updated_at = %s WHERE key IN ('a', 'b', 'c')").format(
                documents=migrated.table("documents")
            ),
            (datetime(2026, 1, 1, tzinfo=UTC),),
        )
    seen: list[str] = []
    before = None
    for _ in range(3):
        page = await documents.list(scope="s", limit=1, before=before)
        assert len(page) == 1
        seen.append(page[0].key)
        before = page[0]
    assert seen == ["c", "b", "a"]
    assert await documents.list(scope="s", limit=1, before=before) == []


async def test_put_with_metadata_replaces_it(documents: PostgresDocumentStore) -> None:
    await documents.put(scope="s", key="k", payload={}, metadata={"title": "a", "extra": 1})
    await documents.put(scope="s", key="k", payload={}, metadata={"title": "b"})
    doc = await documents.get(scope="s", key="k")
    assert doc is not None and doc.metadata == {"title": "b"}


async def test_concurrent_writers_with_one_revision_yield_one_winner(documents: PostgresDocumentStore) -> None:
    await documents.put(scope="s", key="k", payload={"v": 0})
    results = await asyncio.gather(
        *(documents.put(scope="s", key="k", payload={"v": n}, expected_revision=1) for n in range(8)),
        return_exceptions=True,
    )
    assert sum(1 for r in results if r == 2) == 1
    assert sum(1 for r in results if isinstance(r, RevisionConflict)) == 7
    doc = await documents.get(scope="s", key="k")
    assert doc is not None and doc.revision == 2
