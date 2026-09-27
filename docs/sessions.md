# Sessions

`PostgresSessionStore` is a MAF `SessionStore`. It stores one `AgentSession` snapshot per caller-chosen id in `af_sessions`. MAF marks `SessionStore` as experimental. This store follows its interface in `agent-framework-core` 1.19.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `application_id` | `text` | Part of the primary key |
| `session_id` | `text` | The id passed to `set`, `get` and `delete`. Part of the primary key |
| `snapshot` | `jsonb` | `AgentSession.to_dict()`. `NULL` after a tombstone purge |
| `snapshot_version` | `text` | This package's row format, currently `"1"` |
| `revision` | `bigint` | Starts at 1 and grows by one on every `set` |
| `created_at`, `updated_at` | `timestamptz` | First and latest write |
| `expires_at` | `timestamptz` | Set when retention is on |
| `purged_at` | `timestamptz` | Set by a tombstone purge |

## Constructor

```python
PostgresSessionStore(*, application_id, ...)
```

`application_id` is required. The connection arguments and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use).

## Behaviour

- `set(session_id, session)` stores `session.to_dict()` and replaces any earlier snapshot. Custom state types go through MAF's `register_state_type` codecs, as with any session store.
- `get(session_id)` returns a new `AgentSession` built with `AgentSession.from_dict`, or `None` when the id is absent or purged. Changing the returned session does not change the stored one.
- `delete(session_id)` removes the row. Deleting an absent id does nothing.
- An empty or non-`str` id raises `ValueError`, as in MAF's in-memory store.
- `set` rejects state containing `nan` or infinity with `ValueError` before it sends any SQL. MAF's in-memory store accepts such values because it never serializes them.
- Restore a session with the same agent and provider configuration that created it.

## Example

```python
import asyncio
import sys

from agent_framework import AgentSession

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        sessions = hub.session_store()
        session = AgentSession(session_id="conversation-1")
        session.state["turns"] = 3
        await sessions.set("user-1:conversation-1", session)
        restored = await sessions.get("user-1:conversation-1")
        print(restored.session_id if restored else None, restored.state if restored else None)
        await sessions.delete("user-1:conversation-1")
        print(await sessions.get("user-1:conversation-1"))


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

It prints `conversation-1 {'turns': 3}` and then `None`.

## Retention

Each `set` sets `expires_at` to now plus the TTL. In tombstone mode, `purge()` sets `snapshot` to `NULL` and `purged_at` to now, and `get` then returns `None`. The next `set` on that id stores a new snapshot and clears `purged_at`. In delete mode the row is removed. See [retention.md](retention.md).
