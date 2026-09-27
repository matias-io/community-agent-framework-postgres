# Documents and leases

`PostgresDocumentStore` stores JSON documents for application state MAF does not model, keyed by `(collection, scope, key)`, in `af_documents`. Every write increments a revision. Leases in `af_leases` let processes take turns writing one document. MAF has no equivalent of either.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `application_id`, `collection`, `scope`, `key` | `text` | The primary key |
| `payload` | `jsonb` | The document. `NULL` after a tombstone purge |
| `metadata` | `jsonb` | Your own fields, returned by `list`. `{}` by default. Kept by a tombstone purge |
| `revision` | `bigint` | Starts at 1 and grows by one on every write |
| `created_at`, `updated_at` | `timestamptz` | First and latest write |
| `expires_at` | `timestamptz` | Set when retention is on |
| `purged_at` | `timestamptz` | Set by a tombstone purge |

`af_leases` holds `application_id`, `resource`, `owner` and `expires_at`, keyed by `(application_id, resource)`.

## Constructor

```python
PostgresDocumentStore(*, application_id, collection, ...)
```

`application_id` and `collection` are required. The connection arguments and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use). On the hub, call `hub.document_store(collection=...)`.

`scope` is your authorization boundary, for example a user, a tenant or an anonymous visitor. A key alone never reads a row.

## Writing and reading

`put(*, scope, key, payload, metadata=None, expected_revision=None)` returns the new revision.

- `expected_revision=None` writes whether or not the document exists.
- `expected_revision=0` creates the document only when it is absent or purged. A purged row is reused and its revision continues from the old one.
- `expected_revision=n` updates only when the stored revision is `n`.
- A mismatch raises `RevisionConflict` and writes nothing.
- `payload` must be a `dict`, and `metadata` a `dict` or `None`. Anything else raises `TypeError`.
- `metadata=None` keeps the stored metadata. A new document gets `{}`.

`get(*, scope, key)` returns a `Document` (payload, metadata, revision, timestamps) or `None` when absent or purged. `delete(*, scope, key)` returns `True` when a row existed.

`list(*, scope, limit=100, before=None, include_purged=False)` returns `DocumentSummary` objects without payloads, newest `updated_at` first. `limit` must be between 1 and 1000. To get the next page, pass the last summary of the previous page as `before`. Paging uses `(updated_at, key)`, so rows with the same timestamp are never skipped.

## Leases

`lease(*, scope, key, owner, ttl, wait=timedelta(0))` is an async context manager that holds a lease named after the document. The resource name is the JSON array `[collection, scope, key]`. The lease is released when the block exits.

- The database clock decides expiry, so clock drift between processes does not matter.
- The same `owner` can take a lease it already holds.
- With `wait=0`, a lease held by another owner raises `LeaseUnavailable` at once. With a longer `wait`, the store retries with a delay that starts at 0.1 seconds and doubles up to 2 seconds.
- `lease.renew()` extends the lease by `ttl` from now. It raises `LeaseLost` if the lease expired or another owner took it.
- Leases use plain rows, not advisory locks, so they work behind a transaction-mode pooler and never hold a connection during your work.
- Pass a pool or an autocommit connection. Inside a transaction you opened yourself, `now()` does not advance and leases never expire.

`PostgresLeases` (from `hub.leases()`) offers the same leases for any resource name, through `acquire(resource, *, owner, ttl, wait)` and `try_acquire(resource, *, owner, ttl)`, which returns `None` instead of waiting.

## Example

```python
import asyncio
import sys
from datetime import timedelta

from agent_framework_community_postgres import PostgresPersistence, RevisionConflict

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        profiles = hub.document_store(collection="profiles")
        first = await profiles.put(scope="user-1", key="prefs", payload={"theme": "dark"}, expected_revision=0)
        async with profiles.lease(scope="user-1", key="prefs", owner="worker-1", ttl=timedelta(seconds=30)):
            second = await profiles.put(
                scope="user-1", key="prefs", payload={"theme": "light"}, expected_revision=first
            )
        try:
            await profiles.put(scope="user-1", key="prefs", payload={"theme": "blue"}, expected_revision=first)
        except RevisionConflict as exc:
            print("conflict:", exc)
        print(second, [summary.key for summary in await profiles.list(scope="user-1")])
        await profiles.delete(scope="user-1", key="prefs")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

## Retention

Each `put` sets `expires_at` to now plus the TTL. In tombstone mode, `purge()` sets `payload` to `NULL` and keeps the key, metadata, revision and timestamps. `get` then returns `None`, and `list(include_purged=True)` still shows the row. In delete mode the row is removed. A store's `purge()` covers its own collection. Lease rows are not purged. See [retention.md](retention.md).
