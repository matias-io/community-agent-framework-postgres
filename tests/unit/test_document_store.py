from collections.abc import AsyncIterator

import pytest
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres._document_store import PostgresDocumentStore


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool("host=x", open=False)
    yield pool
    await pool.close()


@pytest.mark.parametrize("collection", ["", None])
def test_collection_is_required(collection: object, pool: AsyncConnectionPool) -> None:
    with pytest.raises(ValueError):
        PostgresDocumentStore(application_id="app", collection=collection, client=pool)  # type: ignore[arg-type]


async def test_list_rejects_bad_limits(pool: AsyncConnectionPool) -> None:
    store = PostgresDocumentStore(application_id="app", collection="threads", client=pool)
    with pytest.raises(ValueError):
        await store.list(scope="s", limit=0)


async def test_put_rejects_negative_expected_revision(pool: AsyncConnectionPool) -> None:
    store = PostgresDocumentStore(application_id="app", collection="threads", client=pool)
    with pytest.raises(ValueError):
        await store.put(scope="s", key="k", payload={}, expected_revision=-1)
