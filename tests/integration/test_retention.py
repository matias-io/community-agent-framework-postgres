import pytest
from psycopg import sql

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._retention import purge_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("mode", "tombstone"), [("delete", None), ("tombstone", sql.SQL("snapshot = NULL"))])
async def test_an_or_predicate_never_purges_live_rows(
    client: ClientHandle, migrated: TableNames, mode: str, tombstone: sql.Composable | None
) -> None:
    sessions = migrated.table("sessions")
    async with client.connection() as connection:
        await connection.execute(
            sql.SQL(
                "INSERT INTO {} (application_id, session_id, snapshot, snapshot_version, expires_at)"
                " VALUES ('tests', 'live', '{{}}', '1', now() + interval '1 hour'),"
                " ('tests', 'expired', '{{}}', '1', now() - interval '1 second')"
            ).format(sessions)
        )
        count = await purge_rows(
            connection,
            table=sessions,
            where=sql.SQL("session_id = %(a)s OR session_id = %(b)s"),
            params={"a": "live", "b": "expired"},
            mode=mode,  # type: ignore[arg-type]
            tombstone=tombstone,
        )
        assert count == 1
        cursor = await connection.execute(
            sql.SQL("SELECT session_id FROM {} WHERE snapshot IS NOT NULL ORDER BY session_id").format(sessions)
        )
        assert await cursor.fetchall() == [("live",)]
