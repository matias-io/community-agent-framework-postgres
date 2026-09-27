"""Save a conversation with PostgresHistoryProvider, replay it, and read it back without duplicates."""

import asyncio
import os
import sys

from agent_framework import Message
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    # With no connection_string, the hub reads POSTGRES_CONNECTION_STRING itself.
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        history = hub.history_provider()
        turn = [Message(role="user", contents=["hi"]), Message(role="assistant", contents=["hello"])]
        await history.save_messages("session-1", turn)
        # MAF may pass the whole transcript again. Only the new message is stored.
        await history.save_messages("session-1", [*turn, Message(role="user", contents=["thanks"])])
        print("stored:", [message.text for message in await history.get_messages("session-1")])
        await history.clear("session-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
