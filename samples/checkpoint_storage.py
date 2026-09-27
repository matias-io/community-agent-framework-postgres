"""Run a two-step workflow that checkpoints to PostgresCheckpointStorage."""

import asyncio
import os
import sys

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


class Upper(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str]) -> None:
        await ctx.send_message(text.upper())


class Finish(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str, str]) -> None:
        await ctx.yield_output(text + "!")


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        # scope keeps this conversation's checkpoints apart from other runs of the same workflow.
        storage = hub.checkpoint_storage(scope="conversation-1")
        upper, finish = Upper(id="upper"), Finish(id="finish")
        workflow = WorkflowBuilder(start_executor=upper, checkpoint_storage=storage).add_edge(upper, finish).build()
        result = await workflow.run("hello")
        ids = await storage.list_checkpoint_ids(workflow_name=workflow.name)
        print("output:", result.get_outputs(), "checkpoints:", len(ids))
        for checkpoint_id in ids:
            await storage.delete(checkpoint_id)


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
