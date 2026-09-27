"""Save and read an AG-UI thread snapshot with PostgresAGUIThreadSnapshotStore (needs the ag-ui extra)."""

import asyncio
import os
import sys

from agent_framework_ag_ui import AGUIThreadSnapshot
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        snapshots = hub.thread_snapshot_store()
        snapshot = AGUIThreadSnapshot(messages=[{"id": "m1", "role": "user", "content": "hi"}], state={"step": 1})
        await snapshots.save(scope="user-1", thread_id="thread-1", snapshot=snapshot)
        print("user-1:", await snapshots.get(scope="user-1", thread_id="thread-1"))
        # scope is the authorization boundary. Another scope does not see the thread.
        print("user-2:", await snapshots.get(scope="user-2", thread_id="thread-1"))
        await snapshots.clear(scope="user-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
