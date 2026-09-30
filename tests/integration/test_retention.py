import pytest
from psycopg import sql

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import purge_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("mode", "tombstone"), [("delete", None), ("tombstone", sql.SQL("payload = NULL"))])
async def test_an_or_predicate_never_purges_live_rows(
    client: ClientHandle, migrated: TableNames, mode: str, tombstone: sql.Composable | None
) -> None:
    documents = migrated.table("documents")
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL(
                "INSERT INTO {} (application_id, collection, scope, key, payload, expires_at)"
                " VALUES ('tests', 'c', 's', 'live', '{{}}', now() + interval '1 hour'),"
                " ('tests', 'c', 's', 'expired', '{{}}', now() - interval '1 second')"
            ).format(documents)
        )
        count = await purge_rows(
            connection,
            table=documents,
            where=sql.SQL("key = %(a)s OR key = %(b)s"),
            params={"a": "live", "b": "expired"},
            mode=mode,  # type: ignore[arg-type]
            tombstone=tombstone,
        )
        assert count == 1
        cursor = await connection.execute(
            sql.SQL("SELECT key FROM {} WHERE payload IS NOT NULL ORDER BY key").format(documents)
        )
        assert await cursor.fetchall() == [("live",)]
