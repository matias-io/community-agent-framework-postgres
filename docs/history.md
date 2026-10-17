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
| `max_messages` | `None` | `None` keeps everything, `0` writes nothing, `n` keeps the newest `n` per session. A negative value raises `ValueError` |
| `load_messages`, `store_inputs`, `store_context_messages`, `store_context_from`, `store_outputs` | as in `HistoryProvider` | Passed to MAF unchanged |

The connection arguments (`connection_string`, `client`, `credential`, `env_file_path`, `env_file_encoding`, `pool_kwargs`, `schema`, `table_prefix`) and `retention` are the same on every store. See [connections.md](connections.md).

The key follows `RedisHistoryProvider`. These ids select rows. They do not authorize anyone, so bind them to the signed-in user or tenant in your application.

## Behaviour

- `session_id` must be a non-empty `str`. `None` or `''` raises `ValueError`.
- `save_messages` takes a transaction-scoped advisory lock on the session before it reads, inside a `READ COMMITTED` transaction, so concurrent saves apply MAF's replay rule to the latest stored history whatever the server's default isolation level. The lock key includes the schema and table prefix. On a connection already inside your own transaction, your transaction's isolation level applies and the lock is held until your transaction ends.
- A single `AsyncConnection` runs one call at a time across every store that uses it, so a server should pass a pool.
- A replayed transcript is deduplicated with MAF's `filter_new_messages`. Messages that overlap the end of the stored history are dropped as a replay, and everything else is inserted, including a message the user repeats. The rule is the installed MAF's; see [compatibility.md](compatibility.md#behaviour-maf-owns).
- To deduplicate, a save reads only the newest stored rows, as many as the messages it was given. The result is the same as reading the whole session: MAF 1.20's `filter_new_messages` compares the end of the stored history with the start of the incoming batch, or looks for the whole stored history inside the batch, and both comparisons only look at that many stored rows. A save's cost therefore does not grow with the length of the session.
- If a row in that window is not a JSON object or fails `Message.from_dict`, the save raises `PostgresStorageError` and writes nothing, because it cannot tell which incoming messages are replays. Clear the session or repair the row.
- `get_messages` loads the whole session. A row that is not a JSON object or fails `Message.from_dict` is skipped with a warning on the `agent_framework_community_postgres._history_provider` logger. Set `max_messages` to bound how much a session keeps and loads.
- `list_sessions(*, limit=None, after=None)` returns the session ids under this provider's application, tenant, agent and source, sorted. `limit` caps the page, and `after` starts the page after that id, so pass the last id of one page to get the next.
- `clear(session_id)` deletes one session's rows. It takes the same lock as `save_messages`, so it runs before or after a concurrent save, never in the middle of one.
- When an agent runs without a session, MAF creates a new session id for each call, so every stateless run writes a new history. Pass a session to `agent.run`, or set retention so old histories are purged.

## Use with an agent

Pass the provider in `context_providers` and pass a session to `run`. Before each run the provider loads that session's messages, and after it the provider saves the new ones. This example needs a chat client. It uses `OpenAIChatClient` from `agent-framework-openai` with `OPENAI_API_KEY` set, but any MAF chat client works.

```python
import asyncio
import sys

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        history = hub.history_provider(tenant_id="tenant-1", agent_id="helpdesk")
        agent = Agent(OpenAIChatClient(model="gpt-4o"), "Answer briefly.", context_providers=[history])
        session = agent.create_session(session_id="session-1")
        await agent.run("My name is Ada.", session=session)
        print((await agent.run("What is my name?", session=session)).text)
        print(len(await history.get_messages("session-1")), "messages stored")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

To continue the conversation in another process, store the `AgentSession` with `PostgresSessionStore`, or create a session with the same `session_id`.

## Example without a model

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

Each message is its own row, and its `expires_at` is set once, to its insert time plus the TTL. A purge deletes a session's messages only when none of them is still live. A message saved without a TTL has no `expires_at` and never expires, so it keeps its whole session. An active session keeps its whole transcript, so it never loses its first messages or a tool call's result. Nothing is rewritten when a message is saved. `purge()` on the provider covers its application, tenant, agent and source, across all sessions. History rows are always deleted, never tombstoned. See [retention.md](retention.md).
