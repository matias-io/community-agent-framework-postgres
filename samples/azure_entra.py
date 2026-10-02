"""Sign in to Azure Database for PostgreSQL with Microsoft Entra ID and store one history message.

Needs the ``azure`` extra and an Azure server with Entra authentication enabled. Set
POSTGRES_CONNECTION_STRING to the host, database and role, without a password, for example
``host=my-server.postgres.database.azure.com dbname=agent_framework user=ada@contoso.com``.
DefaultAzureCredential finds a signed-in Azure CLI, environment variables or a managed identity.
"""

import asyncio
import os
import sys

from agent_framework import Message
from azure.identity.aio import DefaultAzureCredential

from agent_framework_community_postgres import PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


async def main() -> None:
    async with (
        DefaultAzureCredential() as credential,
        PostgresPersistence(application_id="samples", schema=SCHEMA, credential=credential) as hub,
    ):
        await hub.migrate()
        history = hub.history_provider()
        await history.save_messages("entra-1", [Message(role="user", contents=["hello from Entra ID"])])
        print([m.text for m in await history.get_messages("entra-1")])
        await history.clear("entra-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
