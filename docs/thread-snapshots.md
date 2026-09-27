# Thread snapshots

`PostgresAGUIThreadSnapshotStore` implements the `AGUIThreadSnapshotStore` protocol from `agent-framework-ag-ui`. It keeps the latest snapshot of each AG-UI thread in `af_thread_snapshots`. It needs the `ag-ui` extra. Importing it without the extra raises `ImportError` with the install command.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `application_id` | `text` | Part of the primary key |
| `scope` | `text` | Your authorization boundary, for example a user id. Part of the primary key |
| `thread_id` | `text` | The AG-UI thread id. Part of the primary key |
| `messages` | `jsonb` | `AGUIThreadSnapshot.messages` |
| `state` | `jsonb` | `AGUIThreadSnapshot.state` |
| `interrupt` | `jsonb` | `AGUIThreadSnapshot.interrupt` |
| `session_state` | `jsonb` | `AGUIThreadSnapshot.session_state`, private server data |
| `revision` | `bigint` | Starts at 1 and grows by one on every `save` |
| `created_at`, `updated_at` | `timestamptz` | First and latest write |
| `expires_at` | `timestamptz` | Set when retention is on |
| `purged_at` | `timestamptz` | Set by a tombstone purge |

## Constructor

```python
PostgresAGUIThreadSnapshotStore(*, application_id, ...)
```

`application_id` is required. The connection arguments and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use).

## Behaviour

- One row per `(scope, thread_id)`. `save` replaces it, so the last writer wins, as in `InMemoryAGUIThreadSnapshotStore`.
- `scope` is part of every lookup. A thread id alone never reads a row, so derive `scope` from the authenticated caller.
- `scope` and `thread_id` must be non-empty strings. Anything else raises `ValueError`. The in-memory store raises `TypeError` for a non-string.
- `get` returns `None` when the thread is absent or purged.
- `delete` returns `True` when a row existed, including a tombstoned one.
- `clear()` removes every snapshot of this application. `clear(scope=...)` removes one scope's.

To use it with the AG-UI endpoint, pass it as `snapshot_store` to `add_agent_framework_fastapi_endpoint`, together with a `snapshot_scope_resolver`.

## Example

```python
import asyncio
import sys

from agent_framework_ag_ui import AGUIThreadSnapshot

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        snapshots = hub.thread_snapshot_store()
        snapshot = AGUIThreadSnapshot(messages=[{"id": "m1", "role": "user", "content": "hi"}], state={"step": 1})
        await snapshots.save(scope="user-1", thread_id="thread-1", snapshot=snapshot)
        print(await snapshots.get(scope="user-1", thread_id="thread-1"))
        print(await snapshots.get(scope="user-2", thread_id="thread-1"))
        await snapshots.clear(scope="user-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

The second `get` prints `None` because `user-2` is a different scope.

## Retention

Each `save` sets `expires_at` to now plus the TTL. In tombstone mode, `purge()` sets `messages`, `state`, `interrupt` and `session_state` to `NULL` and `get` returns `None`. The next `save` stores a new snapshot and clears `purged_at`. In delete mode the row is removed. See [retention.md](retention.md).
