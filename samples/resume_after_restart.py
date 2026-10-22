"""Kill a workflow halfway through, then finish it from its PostgreSQL checkpoint in a new process.

Run the script twice:

    python samples/resume_after_restart.py crash    # step one runs and is checkpointed, then the process dies
    python samples/resume_after_restart.py resume   # a new process loads the checkpoint and runs step two
"""

import asyncio
import os
import sys

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler
from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresCheckpointStorage, PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")


class Upper(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str]) -> None:
        print("step one: upper-casing", repr(text), flush=True)
        await ctx.send_message(text.upper())


class Finish(Executor):
    def __init__(self, *, crash: bool) -> None:
        super().__init__(id="finish")
        self._crash = crash

    @handler
    async def run(self, text: str, ctx: WorkflowContext[str, str]) -> None:
        if self._crash:
            print("step two: the process dies before it finishes", flush=True)
            os._exit(1)  # no cleanup, as in a real crash
        await ctx.yield_output(text + "!")


def build(storage: PostgresCheckpointStorage, *, crash: bool) -> Workflow:
    # A fixed name: the new process looks the checkpoint up by it.
    upper, finish = Upper(id="upper"), Finish(crash=crash)
    return (
        WorkflowBuilder(name="order-pipeline", start_executor=upper, checkpoint_storage=storage)
        .add_edge(upper, finish)
        .build()
    )


async def main(command: str) -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        # One scope per run, such as an order or conversation id, keeps its checkpoints apart.
        storage = hub.checkpoint_storage(scope="order-42")
        if command == "crash":
            await build(storage, crash=True).run("hello")
            return
        workflow = build(storage, crash=False)
        latest = await storage.get_latest(workflow_name=workflow.name)
        if latest is None:
            print("nothing to resume; run the 'crash' command first")
            return
        result = await workflow.run(checkpoint_id=latest.checkpoint_id)
        print("resumed from", latest.checkpoint_id, "output:", result.get_outputs())
        for checkpoint_id in await storage.list_checkpoint_ids(workflow_name=workflow.name):
            await storage.delete(checkpoint_id)


if len(sys.argv) != 2 or sys.argv[1] not in ("crash", "resume"):
    sys.exit(__doc__)
with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main(sys.argv[1]))
