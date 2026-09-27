"""Write a document under a lease with optimistic concurrency using PostgresDocumentStore."""

import asyncio
import os
import sys
from datetime import timedelta

from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence, RevisionConflict

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        profiles = hub.document_store(collection="profiles")
        first = await profiles.put(scope="user-1", key="prefs", payload={"theme": "dark"}, expected_revision=0)
        async with profiles.lease(scope="user-1", key="prefs", owner="worker-1", ttl=timedelta(seconds=30)):
            second = await profiles.put(
                scope="user-1", key="prefs", payload={"theme": "light"}, expected_revision=first
            )
        print("revisions:", first, second)
        try:
            await profiles.put(scope="user-1", key="prefs", payload={"theme": "blue"}, expected_revision=first)
        except RevisionConflict as exc:
            print("stale write refused:", exc)
        document = await profiles.get(scope="user-1", key="prefs")
        print("stored:", document.payload if document else None)
        await profiles.delete(scope="user-1", key="prefs")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
