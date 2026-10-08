# Leases

A lease serializes work on one resource across replicas. MAF hosting serializes runs per process only. `PostgresLeases` stores leases in `af_leases` and lets processes take turns on a resource name you choose. MAF has no equivalent.

## Columns

`af_leases` holds `application_id`, `resource`, `owner`, `token` and `expires_at`, keyed by `(application_id, resource)`. `token` is new on every acquisition.

## Constructor

```python
PostgresLeases(*, application_id, ...)
```

`application_id` is required. The connection arguments are the same on every store. See [Standalone use](../README.md#standalone-use). On the hub, call `hub.leases()`.

## Acquiring

`acquire(resource, *, owner, ttl, wait=timedelta(0))` is an async context manager that holds the lease on `resource`. The lease is released when the block exits. If the block raises and the release then fails, for example because the database is down, a warning is logged on the `agent_framework_community_postgres._leases` logger and the block's own exception propagates. The lease then frees itself when its `ttl` passes.

`try_acquire(resource, *, owner, ttl)` takes the lease once and returns a `Lease`, or `None` when it is held. You release that lease yourself with `lease.release()`.

- The database clock decides expiry, so clock drift between processes does not matter.
- A lease has one holder per acquisition. While it is held, every other acquisition waits or fails, including one with the same `owner`. `owner` is a label for diagnostics, and `renew` and `release` match the acquisition's own `token`.
- A holder that crashed without releasing is recovered by expiry: once its `ttl` has passed, the next acquisition takes the lease.
- With `wait=0`, a lease that is held raises `LeaseUnavailable` at once. With a longer `wait`, `acquire` retries with a delay that starts at 0.1 seconds and doubles up to 2 seconds.
- `lease.renew()` extends the lease by `ttl` from now. It raises `LeaseLost` if the lease expired or another acquisition took it.
- Releasing a lease you no longer hold does nothing.
- Leases use plain rows, not advisory locks, so they never hold a connection during your work. Behind a transaction-mode pooler such as PgBouncer, pass a pool with `prepare_threshold=None`. See [Standalone use](../README.md#standalone-use).
- Pass a pool or an autocommit connection. A single connection runs one call at a time, so a server should pass a pool. Inside a transaction you opened yourself, `now()` does not advance and leases never expire.

`LeaseUnavailable` and `LeaseLost` carry the resource name as `resource` and never put it in the message, because a resource name often contains a user or visitor id.

## What a lease does not do

A lease serializes *acquisition*: one holder at a time, judged by the database clock. It does not fence the holder's later writes. A holder that pauses past `ttl` (a long model call, a stopped process) loses the lease without noticing, and a write it makes afterwards lands next to the new holder's. Renew well inside `ttl`, keep the work under a lease short, and for a write that must not race carry your own version check in the row you write.

## Example

```python
import asyncio
import sys
from datetime import timedelta

from agent_framework_community_postgres import LeaseUnavailable, PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        leases = hub.leases()
        async with leases.acquire("thread/t1", owner="worker-1", ttl=timedelta(seconds=30)) as lease:
            try:
                async with leases.acquire("thread/t1", owner="worker-2", ttl=timedelta(seconds=30)):
                    pass
            except LeaseUnavailable as exc:
                print("held:", exc.resource)
            await lease.renew()
        released = await leases.try_acquire("thread/t1", owner="worker-2", ttl=timedelta(seconds=30))
        if released is not None:
            print("taken by", released.owner)
            await released.release()


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

## Retention

An expired lease row stays until the next acquisition of the same resource replaces it or a purge deletes it. `leases.purge()` deletes the expired leases of its `application_id` and returns a `PurgeReport` with an `af_leases` count. `hub.purge()` and the `purge` CLI command do the same. An expired lease is already free, so a purge never takes a lease from a holder. See [retention.md](retention.md).
