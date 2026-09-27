"""Store an AgentSession with PostgresSessionStore and restore it."""

import asyncio
import os
import sys

from agent_framework import AgentSession
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        sessions = hub.session_store()
        session = AgentSession(session_id="conversation-1")
        session.state["turns"] = 3
        await sessions.set("user-1:conversation-1", session)
        restored = await sessions.get("user-1:conversation-1")
        print("restored:", restored.session_id if restored else None, restored.state if restored else None)
        await sessions.delete("user-1:conversation-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
