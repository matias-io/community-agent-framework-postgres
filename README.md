# community-agent-framework-postgres

PostgreSQL storage for Microsoft Agent Framework (MAF). The package provides `PostgresHistoryProvider` for chat history, `PostgresSessionStore` for `AgentSession` snapshots, `PostgresCheckpointStorage` for workflow checkpoints, `PostgresAGUIThreadSnapshotStore` for AG-UI thread snapshots and `PostgresDocumentStore` for JSON documents with revisions and leases. `PostgresPersistence` creates all of them over one connection pool and runs the schema migrations. This is a community package written by Matias Suxo Salinas. Microsoft does not maintain or support it.

## Status

Alpha. Public names and constructor arguments may change in a 0.x minor release, and `CHANGELOG.md` lists every such change. Schema changes ship as new numbered migrations, so `migrate()` upgrades an existing database in place.

## Install

```bash
uv add community-agent-framework-postgres
uv add "community-agent-framework-postgres[ag-ui]"  # adds PostgresAGUIThreadSnapshotStore
pip install community-agent-framework-postgres
pip install "community-agent-framework-postgres[ag-ui]"
```

The `ag-ui` extra installs `agent-framework-ag-ui`. Without it, every store except `PostgresAGUIThreadSnapshotStore` works.

On Windows, async psycopg cannot run on the default `ProactorEventLoop`. Run a script's entry point on a selector loop:

```python
with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Run uvicorn 0.36 or later with `--loop asyncio:SelectorEventLoop`. uvicorn imports that value as the loop factory. Its plain `--loop asyncio` picks `ProactorEventLoop` on Windows unless reload or workers are on. Use `127.0.0.1` rather than `localhost` in local connection strings. libpq tries `::1` first, and some Windows and WSL setups drop that traffic without an error.

## Quick start

This script runs against the database from `docker-compose.yml`. It needs the `ag-ui` extra.

```python
import asyncio
import sys

from agent_framework import AgentSession, Message

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="quickstart", connection_string=DSN) as hub:
        await hub.migrate()
        history = hub.history_provider()
        await history.save_messages("session-1", [Message(role="user", contents=["hello"])])
        print([m.text for m in await history.get_messages("session-1")])
        await hub.session_store().set("session-1", AgentSession(session_id="session-1"))
        print(await hub.checkpoint_storage(scope="session-1").list_checkpoint_ids(workflow_name="demo"))
        print(await hub.thread_snapshot_store().get(scope="user-1", thread_id="thread-1"))
        print(await hub.document_store(collection="notes").put(scope="user-1", key="n1", payload={"text": "hi"}))


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Every store the hub creates borrows its pool and its `application_id`. It also takes the hub's `schema`, `table_prefix` and `retention` unless you pass your own to the factory. The factories raise `TypeError` if you pass `application_id` or `client`. `hub.purge()` still covers a store with its own `retention`. It removes that store's rows whose `expires_at` has passed, in the hub's mode. After `hub.close()`, every store it created raises `PostgresStorageError`, even when the hub was built over your own pool.

## Use with Agent Framework

Give an agent the history provider in `context_providers` and pass a session to `run`. The provider loads and saves that session's messages. This snippet needs a chat client. It uses `OpenAIChatClient` from `agent-framework-openai` with `OPENAI_API_KEY` set, but any MAF chat client works.

```python
import asyncio
import sys

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="my-app", connection_string=DSN) as hub:
        await hub.migrate()
        agent = Agent(OpenAIChatClient(model="gpt-4o"), "Answer briefly.", context_providers=[hub.history_provider()])
        session = agent.create_session()
        print((await agent.run("My name is Ada.", session=session)).text)
        print((await agent.run("What is my name?", session=session)).text)


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Pass the checkpoint storage to `WorkflowBuilder`. The workflow then writes checkpoints you can resume from. [docs/checkpoints.md](docs/checkpoints.md) has the full example.

```python
import asyncio
import sys

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


class Shout(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str, str]) -> None:
        await ctx.yield_output(text.upper())


async def main() -> None:
    async with PostgresPersistence(application_id="my-app", connection_string=DSN) as hub:
        await hub.migrate()
        storage = hub.checkpoint_storage(scope="conversation-1")
        workflow = WorkflowBuilder(start_executor=Shout(id="shout"), checkpoint_storage=storage).build()
        print((await workflow.run("hello")).get_outputs())
        print(await storage.get_latest(workflow_name=workflow.name) is not None)


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Save the `AgentSession` with `PostgresSessionStore` to continue a conversation in another process. An application loads the session at the start of a request and saves it after `agent.run` returns.

```python
import asyncio
import sys

from agent_framework import AgentSession

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="my-app", connection_string=DSN) as hub:
        await hub.migrate()
        sessions = hub.session_store()
        session = await sessions.get("user-1:chat-1") or AgentSession(session_id="chat-1")
        session.state["visits"] = session.state.get("visits", 0) + 1
        await sessions.set("user-1:chat-1", session)
        print(session.session_id, session.state)


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

In an agent, create the new session with `agent.create_session(session_id=...)` instead of `AgentSession(...)`. See [docs/history.md](docs/history.md) and [docs/sessions.md](docs/sessions.md).

## Standalone use

Each store also works on its own. A standalone store does not migrate, so create the tables first with `hub.migrate()` or the CLI.

```bash
POSTGRES_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework python -m agent_framework_community_postgres migrate
```

In PowerShell:

```powershell
$env:POSTGRES_CONNECTION_STRING = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"; python -m agent_framework_community_postgres migrate
```

```python
import asyncio
import sys

from agent_framework import Message

from agent_framework_community_postgres import PostgresHistoryProvider

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresHistoryProvider(application_id="my-app", connection_string=DSN) as history:
        await history.save_messages("session-1", [Message(role="user", contents=["hello"])])
        print([m.text for m in await history.get_messages("session-1")])


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Every store and the hub take the same connection arguments.

- `connection_string` or `client`. Pass exactly one. When both are absent, the connection string comes from `POSTGRES_CONNECTION_STRING`, read through MAF's `load_settings`. An explicit argument wins over the `.env` file named by `env_file_path`, which wins over the environment.
- `client` is a psycopg `AsyncConnection` or `AsyncConnectionPool`. A pool that is not open is opened on first use. The package never closes a client it did not create.
- A single `AsyncConnection` runs one call at a time. Every store and hub over it waits for the same lock, so a server should pass a pool. On a connection already inside your own transaction, each call becomes a savepoint, and transaction-scoped locks (the history save lock and the migration lock) are held until your transaction ends.
- A connection string makes the store own a pool with `min_size=1`, `max_size=10`, autocommit, libpq `connect_timeout=10` unless the string sets one, and a pool `timeout` of 10 seconds.
- `schema="public"` must be a lowercase identifier that does not start with `pg_`. `table_prefix="af_"` allows lowercase letters, digits and underscores, up to 35 bytes.

When the database is down or rejects the login, each call raises `PostgresStorageError("Could not connect to PostgreSQL within 10 seconds; ...")` after about 10 seconds. The owned pool keeps retrying in the background and recovers when the database returns. psycopg_pool logs the driver's reason on the `psycopg.pool` logger. If you need other timeouts, pass your own pool. A malformed connection string raises `PostgresStorageError("Invalid connection string.")` and never repeats libpq's text, which can contain the password.

## Schema

Tables are created in `schema` and named `{table_prefix}{name}`. With the defaults:

| Table | Key | Holds |
|---|---|---|
| `af_history_messages` | `id`, read by application, tenant, agent, source and session | One `Message.to_dict()` per row |
| `af_sessions` | `application_id, session_id` | `AgentSession.to_dict()` |
| `af_checkpoints` | `application_id, scope, checkpoint_id` | The encoded `WorkflowCheckpoint` |
| `af_thread_snapshots` | `application_id, scope, thread_id` | The latest AG-UI thread snapshot |
| `af_documents` | `application_id, collection, scope, key` | Payload, metadata and revision |
| `af_leases` | `application_id, resource` | Lease owner and expiry |
| `af_migrations` | `version` | Applied migration versions |

## Retention

Retention is off unless you pass `RetentionPolicy(ttl=...)`. With a TTL, every write sets `expires_at` to now plus the TTL, and `purge()` or the `purge` CLI command tombstones or deletes the rows whose `expires_at` has passed. History messages and checkpoints are always deleted. See [docs/retention.md](docs/retention.md).

## Compatibility

| Package | agent-framework-core | agent-framework-ag-ui | Python | PostgreSQL |
|---|---|---|---|---|
| 0.1.0 | >=1.19.0,<2 | >=1.4.0,<2 (extra) | 3.11 to 3.14 | 16, 17 |

## Private Agent Framework imports

The package uses three names MAF does not export. `filter_new_messages` deduplicates history, and `encode_checkpoint_value` and `decode_checkpoint_value` encode checkpoints. Microsoft's own Cosmos DB and Redis packages import the same names. `_framework.py` is the only module that imports them, and `tests/unit/test_framework.py` fails when one of them moves. CI also runs the suite against the newest `agent-framework-core` release, so an upstream rename shows up there before users upgrade. That job does not block a merge. See [docs/compatibility.md](docs/compatibility.md).

## Development

```bash
docker compose up -d --wait
export POSTGRES_TEST_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework
# PowerShell: $env:POSTGRES_TEST_CONNECTION_STRING = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Without `POSTGRES_TEST_CONNECTION_STRING`, the integration and conformance tests are skipped. See `CONTRIBUTING.md`.

## Documentation

- [docs/history.md](docs/history.md) covers `PostgresHistoryProvider`.
- [docs/sessions.md](docs/sessions.md) covers `PostgresSessionStore`.
- [docs/checkpoints.md](docs/checkpoints.md) covers `PostgresCheckpointStorage`.
- [docs/thread-snapshots.md](docs/thread-snapshots.md) covers `PostgresAGUIThreadSnapshotStore`.
- [docs/documents-and-leases.md](docs/documents-and-leases.md) covers `PostgresDocumentStore` and `PostgresLeases`.
- [docs/retention.md](docs/retention.md) covers `RetentionPolicy` and purging.
- [docs/migrations.md](docs/migrations.md) covers `migrate()`, the CLI and SQL for a DBA.
- [docs/compatibility.md](docs/compatibility.md) covers versions, private imports, JSONB limits and Windows.

Runnable scripts for each store are in `samples/`.

## License

MIT. See `LICENSE`.
