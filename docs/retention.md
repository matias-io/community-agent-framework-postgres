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

Each write sets `expires_at` to the database's `now()` plus `ttl`. What that means depends on the table.

- `af_sessions` and `af_thread_snapshots` keep one row per key and rewrite it. Expiry counts from the last write to that key.
- `af_history_messages` adds one row per message. Each message's `expires_at` is a TTL after it was inserted and is never rewritten. A purge deletes a session's messages only when every one of them has expired, so an active session keeps its whole transcript. A message written while `ttl` was `None` has no expiry and keeps its whole session.
- `af_checkpoints` adds one row per checkpoint. Each checkpoint expires a TTL after its save, so a long-running workflow loses its oldest checkpoints first. Saving the same `checkpoint_id` again resets that row.

Rows written while `ttl` was `None` have no `expires_at` and are never purged. Changing the policy does not touch existing rows until they are written again. A purge acts on the `expires_at` already stored, so the object that runs it needs no policy of its own.

## What a purge does

A purge acts on rows whose `expires_at` has passed. A history session is purged only when no message of the session is still live, and a message with no `expires_at` is always live; until then none of its messages are touched.

| Table | Tombstone mode | Delete mode |
|---|---|---|
| `af_history_messages` | Deleted, a whole session at a time | Deleted, a whole session at a time |
| `af_sessions` | `snapshot` set to `NULL` | Deleted |
| `af_checkpoints` | Deleted | Deleted |
| `af_thread_snapshots` | `messages`, `state`, `interrupt`, `session_state` set to `NULL` | Deleted |
| `af_leases` | Deleted | Deleted |

A tombstone keeps the ids and timestamps and sets `purged_at`. Reads treat a tombstoned row as absent. The next write to the same key stores new data and clears `purged_at`. A row that is already tombstoned is not counted again. Delete mode also removes rows an earlier purge tombstoned.

A purge works in batches of 1000 rows, one transaction per batch. For sessions, checkpoints, AG-UI snapshots and leases, each batch locks its rows with `FOR UPDATE SKIP LOCKED`, so two purges running at once take different rows and never wait on each other, and it keeps going until a batch comes back short. A failure part way keeps the batches already committed.

History is purged a session at a time, so a session is never split across batches. Each batch finds the sessions of up to 1000 expired messages whose sessions have no live message and tries each session's save lock, the lock `save_messages` and `clear` take. It deletes the sessions it locked, after checking again that none of their messages is live, and skips a session whose lock another writer holds; a later purge takes it. It keeps going until a round finds no session left to delete. A save that lands while a purge runs therefore keeps its whole session, and two purges never delete the same session twice or wait on each other.

Measured on 2026-10-19 against PostgreSQL 17.11 in Docker on Windows, 100k rows / 1k sessions: 100 history rows per session, every row expired except one in each of 50 sessions. A full `hub.purge()` took 1.98 and 2.02 s over two runs and deleted 95,000 rows, leaving the 50 sessions with a live row whole. The 0.1.1 purge, which batched rows rather than sessions and took no session lock, took 1.47 and 1.78 s on the same machine and data.

Finding the candidates is an anti join of the expired rows against their sessions' live rows, both read through `af_history_messages_session_expiry_idx`, so its cost grows with the number of live rows in the scope being purged, not with the batch size. A scope holding millions of live history rows makes every batch correspondingly slower; measure on your own data before scheduling a purge against a table that size.

An expired lease is already free, so deleting it changes nothing for holders. `hub.purge()` and `PostgresLeases.purge()` both delete expired leases.

`purge()` returns a `PurgeReport`. `report.counts` maps each table name to the rows it changed, and `report.total` is the sum. `counts` is a read-only mapping, not a `dict`; `dict(report.counts)` gives a copy you can change. A report pickles, copies and works with `dataclasses.asdict`.

## Scope of a purge

- `hub.purge()` covers every row of the hub's `application_id` in every table. That includes all history sources, tenants and agents, and all checkpoint scopes, and the hub's leases.
- A store's `purge()` covers that store's rows only. The history provider covers its application, tenant, agent and source. A checkpoint storage covers its scope.
- A purge acts on every expired row in its scope, whichever policy stamped it. A hub or store built without a policy still purges rows another store wrote with one.
- `hub.purge(mode=None)` uses `mode` when you pass it, otherwise the hub policy's mode, which defaults to `"tombstone"`. A store's `purge()` uses its own policy's mode.

## Example

```python
import asyncio
import sys
from datetime import timedelta

from agent_framework import AgentSession

from agent_framework_community_postgres import PostgresPersistence, RetentionPolicy

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    policy = RetentionPolicy(ttl=timedelta(seconds=1))
    async with PostgresPersistence(application_id="docs", connection_string=DSN, retention=policy) as hub:
        await hub.migrate()
        sessions = hub.session_store()
        await sessions.set("session-1", AgentSession(session_id="session-1"))
        await asyncio.sleep(1.5)
        report = await hub.purge()
        print(report.counts["af_sessions"], await sessions.get("session-1"))
        await sessions.delete("session-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

It prints `1 None`.

## Running purges on a schedule

The CLI runs the same purge as `hub.purge()`. This cron entry purges every night at 3 a.m.:

```bash
0 3 * * * POSTGRES_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework python -m agent_framework_community_postgres purge --application-id my-app --mode tombstone
```

`--application-id` is required. The rows come from the `expires_at` your application's policy set when it wrote them, so the command takes no TTL. `--mode` defaults to `tombstone`. The command prints one line per table and a total.
