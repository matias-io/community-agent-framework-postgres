# History

`PostgresHistoryProvider` is a MAF `HistoryProvider`. It stores one row per message in `af_history_messages` and loads a session's messages before each agent run.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | `bigint` identity | Insertion order |
| `application_id` | `text` | The provider's `application_id` |
| `tenant_id` | `text` | `tenant_id`, or `''` when not set |
| `agent_id` | `text` | `agent_id`, or `''` when not set |
| `source_id` | `text` | The provider's `source_id` |
| `session_id` | `text` | The MAF session id |
| `message` | `jsonb` | `Message.to_dict()` |
| `created_at` | `timestamptz` | Write time |
| `expires_at` | `timestamptz` | Set when retention is on |

## Constructor

```python
PostgresHistoryProvider(source_id="postgres_history", *, application_id, ...)
```

| Argument | Default | Meaning |
|---|---|---|
| `source_id` | `"postgres_history"` | MAF provider id, also part of the row key |
| `application_id` | required | Top level of the row key |
| `tenant_id` | `None` | Optional tenant in the row key. `''` raises `ValueError` |
| `agent_id` | `None` | Optional agent in the row key. `''` raises `ValueError` |
| `max_messages` | `None` | `None` keeps everything, `0` writes nothing, `n` keeps the newest `n` per session |
| `load_messages`, `store_inputs`, `store_context_messages`, `store_context_from`, `store_outputs` | as in `HistoryProvider` | Passed to MAF unchanged |

The connection arguments (`connection_string`, `client`, `env_file_path`, `env_file_encoding`, `schema`, `table_prefix`) and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use).

The key follows `RedisHistoryProvider`. These ids select rows. They do not authorize anyone, so bind them to the signed-in user or tenant in your application.

## Behaviour

- `session_id` must be a non-empty `str`. `None` or `''` raises `ValueError`.
- `save_messages` takes a transaction-scoped advisory lock on the session before it reads, so concurrent saves of the same turn store it once.
- A replayed transcript is deduplicated with MAF's `filter_new_messages`. Only the messages after the stored ones are inserted.
- A row that is not a JSON object or fails `Message.from_dict` is skipped with a warning on the `agent_framework_community_postgres._history_provider` logger.
- `list_sessions()` returns the session ids under this provider's application, tenant, agent and source, sorted.
- `clear(session_id)` deletes one session's rows.
- When an agent runs without a session, MAF creates a new session id for each call, so every stateless run writes a new history. Pass a session to `agent.run`, or set retention so old histories are purged.

Pass the provider to an agent in `context_providers`, as with any `HistoryProvider`.

## Example

```python
import asyncio
import sys

from agent_framework import Message

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        history = hub.history_provider(tenant_id="tenant-1", agent_id="helpdesk", max_messages=50)
        turn = [Message(role="user", contents=["hi"]), Message(role="assistant", contents=["hello"])]
        await history.save_messages("session-1", turn)
        await history.save_messages("session-1", [*turn, Message(role="user", contents=["thanks"])])
        print([m.text for m in await history.get_messages("session-1")])
        print(await history.list_sessions())
        await history.clear("session-1")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

It prints `['hi', 'hello', 'thanks']` and then `['session-1']`.

## Retention

Each insert sets `expires_at` to now plus the TTL. `purge()` on the provider deletes expired rows under its application, tenant, agent and source, across all sessions. History rows are always deleted, never tombstoned. See [retention.md](retention.md).
