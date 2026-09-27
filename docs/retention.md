# Retention

Retention is off by default. Nothing in this package expires a row by itself. You set a policy, and a purge you run applies it.

## Policy

```python
RetentionPolicy(ttl=None, mode="tombstone")
```

| Argument | Default | Meaning |
|---|---|---|
| `ttl` | `None` | A positive `timedelta`. `None` means writes set no expiry |
| `mode` | `"tombstone"` | `"tombstone"` keeps the row and drops its payload. `"delete"` removes the row |

A `ttl` of zero or less, or another `mode`, raises `ValueError`. Pass the policy as `retention=` to a store or to `PostgresPersistence`, which hands it to every store it creates.

## What a write does

Each write sets `expires_at` to the database's `now()` plus `ttl`. Expiry therefore counts from the last write, not from creation. Rows written while `ttl` was `None` have no `expires_at` and are never purged. Changing the policy does not touch existing rows until they are written again.

## What a purge does

A purge acts on rows whose `expires_at` has passed.

| Table | Tombstone mode | Delete mode |
|---|---|---|
| `af_history_messages` | Deleted | Deleted |
| `af_sessions` | `snapshot` set to `NULL` | Deleted |
| `af_checkpoints` | Deleted | Deleted |
| `af_thread_snapshots` | `messages`, `state`, `interrupt`, `session_state` set to `NULL` | Deleted |
| `af_documents` | `payload` set to `NULL` | Deleted |
| `af_leases` | Not purged | Not purged |

A tombstone keeps the ids, timestamps, revision and metadata and sets `purged_at`. Reads treat a tombstoned row as absent. The next write to the same key stores new data and clears `purged_at`. A row that is already tombstoned is not counted again.

`purge()` returns a `PurgeReport`. `report.counts` maps each table name to the rows it changed, and `report.total` is the sum.

## Scope of a purge

- `hub.purge()` covers every row of the hub's `application_id` in every table. That includes all history sources, tenants and agents, all checkpoint scopes and all document collections.
- A store's `purge()` covers that store's rows only. The history provider covers its application, tenant, agent and source. A checkpoint storage covers its scope. A document store covers its collection.
- With no `ttl`, `purge()` returns an empty report without running SQL.

## Example

```python
import asyncio
import sys
from datetime import timedelta

from agent_framework_community_postgres import PostgresPersistence, RetentionPolicy

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    policy = RetentionPolicy(ttl=timedelta(seconds=1))
    async with PostgresPersistence(application_id="docs", connection_string=DSN, retention=policy) as hub:
        await hub.migrate()
        notes = hub.document_store(collection="notes")
        await notes.put(scope="user-1", key="n1", payload={"text": "hi"}, metadata={"title": "first"})
        await asyncio.sleep(1.5)
        report = await hub.purge()
        print(report.counts["af_documents"], await notes.get(scope="user-1", key="n1"))
        print([s.metadata for s in await notes.list(scope="user-1", include_purged=True)])
        await notes.delete(scope="user-1", key="n1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

It prints `1 None` and then `[{'title': 'first'}]`.

## Running purges on a schedule

The CLI runs the same purge as `hub.purge()`. This cron entry purges every night at 3 a.m.:

```bash
0 3 * * * POSTGRES_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework python -m agent_framework_community_postgres purge --application-id my-app --ttl 2592000 --mode tombstone
```

`--application-id` is required. `--ttl` must be a positive number of seconds and turns purging on, but it does not choose the rows. The rows come from the `expires_at` your application's policy set when it wrote them. `--mode` defaults to `tombstone`. The command prints one line per table and a total.
