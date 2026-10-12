"""Hold a PostgresLeases lease, renew it, and hand it to a second worker that waits for it."""

import asyncio
import os
import sys
from datetime import timedelta

from psycopg import AsyncConnection, sql

from agent_framework_community_postgres import PostgresLeases, PostgresPersistence

SCHEMA = os.environ.get("SAMPLE_SCHEMA", "public")
TTL = timedelta(seconds=30)


async def worker_1(leases: PostgresLeases, started: asyncio.Event) -> None:
    async with leases.acquire("thread/t1", owner="worker-1", ttl=TTL) as lease:
        started.set()
        await asyncio.sleep(0.5)
        # Renew well inside the TTL while the work goes on; the lease is released when the block exits.
        await lease.renew()
        await asyncio.sleep(0.5)


async def worker_2(leases: PostgresLeases, started: asyncio.Event) -> None:
    await started.wait()
    # The lease is held, so this retries until worker-1 releases it or 5 seconds pass.
    async with leases.acquire("thread/t1", owner="worker-2", ttl=TTL, wait=timedelta(seconds=5)) as lease:
        print("held by:", lease.owner)


async def main() -> None:
    async with await AsyncConnection.connect(os.environ["POSTGRES_CONNECTION_STRING"], autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    async with PostgresPersistence(application_id="samples", schema=SCHEMA) as hub:
        await hub.migrate()
        leases = hub.leases()
        started = asyncio.Event()
        await asyncio.gather(worker_1(leases, started), worker_2(leases, started))


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
