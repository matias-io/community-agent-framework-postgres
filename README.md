# community-agent-framework-postgres

PostgreSQL storage for Microsoft Agent Framework (MAF). The package provides `PostgresHistoryProvider` for chat history, `PostgresSessionStore` for `AgentSession` snapshots, `PostgresCheckpointStorage` for workflow checkpoints, `PostgresAGUIThreadSnapshotStore` for AG-UI thread snapshots and `PostgresDocumentStore` for JSON documents with revisions and leases. `PostgresPersistence` creates all of them over one connection pool and runs the schema migrations. This is a community package written by Matias Suxo Salinas. Microsoft does not maintain or support it.

## Status

Alpha. Public names and constructor arguments may change in a 0.x minor release, and `CHANGELOG.md` lists every such change. Schema changes ship as new numbered migrations, so `migrate()` upgrades an existing database in place.

## Compatibility

| | Supported in 0.1.1 |
|---|---|
| `agent-framework-core` | 1.19.x to 1.20.x (`>=1.19.0,<1.21`) |
| `agent-framework-ag-ui` | 1.4.x to 1.5.x (`>=1.4.0,<1.6`), through the `ag-ui` extra |
| Python | 3.11 to 3.14 |
| Python 3.15 | Smoke-tested on 3.15.0b4, not yet supported |
| PostgreSQL | 16, 17 |
| psycopg | 3.3 and later (`>=3.3.5,<4`) |

The MAF bounds are the tested range, and a patch release widens them after a new MAF minor passes CI. See [docs/compatibility.md](docs/compatibility.md) for the support policy.

## What it implements

| Class | MAF interface | Extra | Conditions |
|---|---|---|---|
| `PostgresHistoryProvider` | `HistoryProvider` | none | Drops replayed messages with the installed MAF's own rule, which changed in 1.20 |
| `PostgresSessionStore` | `SessionStore` | none | MAF marks `SessionStore` experimental |
| `PostgresCheckpointStorage` | `CheckpointStorage` | none | Decodes only MAF's safe types and its own; `allowed_checkpoint_types` adds more |
| `PostgresAGUIThreadSnapshotStore` | `AGUIThreadSnapshotStore` | `ag-ui` | The AG-UI endpoint needs a `snapshot_scope_resolver` next to `snapshot_store` |
| `PostgresDocumentStore`, `PostgresLeases` | none (this package's own) | none | |
| `PostgresPersistence` | none (creates the stores above over one pool) | none | |
| Microsoft Entra ID sign-in (`credential=`) | none | `azure` | Azure Database for PostgreSQL with Entra authentication |

## Install

```bash
uv add community-agent-framework-postgres
uv add "community-agent-framework-postgres[ag-ui]"  # adds PostgresAGUIThreadSnapshotStore
uv add "community-agent-framework-postgres[azure]"  # adds Microsoft Entra ID sign-in
pip install community-agent-framework-postgres
pip install "community-agent-framework-postgres[ag-ui]"
pip install "community-agent-framework-postgres[azure]"
```

The `ag-ui` extra installs `agent-framework-ag-ui`. Without it, every store except `PostgresAGUIThreadSnapshotStore` works. `from agent_framework_community_postgres import *` also needs the extra, because `__all__` lists that store. The `azure` extra installs `azure-identity`, and `aiohttp` for its async credentials, for [Microsoft Entra ID sign-in](#microsoft-entra-id-on-azure).

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

Every store the hub creates borrows its pool and its `application_id`. It also takes the hub's `schema`, `table_prefix` and `retention` unless you pass your own to the factory. The factories raise `TypeError` if you pass `application_id`, `client` or `credential`. `hub.purge()` covers every store, including one with its own `retention`, and needs no policy of its own. It purges the rows whose `expires_at` has passed, in the mode you pass or else the hub policy's mode. After `hub.close()`, every store it created raises `PostgresStorageError`, even when the hub was built over your own pool.

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
- `credential` signs the owned pool in to Azure Database for PostgreSQL with Microsoft Entra ID tokens. It needs a connection string and cannot be combined with `client`. See [Microsoft Entra ID on Azure](#microsoft-entra-id-on-azure).
- A single `AsyncConnection` runs one call at a time. Every store and hub over it waits for the same lock, so a server should pass a pool. On a connection already inside your own transaction, each call becomes a savepoint, and transaction-scoped locks (the history save lock and the migration lock) are held until your transaction ends.
- A connection string makes the store own a pool with `min_size=1`, `max_size=10`, autocommit, libpq `connect_timeout=10` unless the string sets one, and a pool `timeout` of 10 seconds.
- `schema="public"` must be a lowercase identifier that does not start with `pg_`. `table_prefix="af_"` allows lowercase letters, digits and underscores, up to 35 bytes.

When the database is down or rejects the login, or every pooled connection stays busy, each call raises `PostgresStorageError("No PostgreSQL connection became available within 10 seconds: ...")` after about 10 seconds. psycopg_pool raises the same timeout in both cases, so the message names both. Over your own pool, the message gives that pool's `timeout`. The owned pool keeps retrying in the background and recovers when the database returns. psycopg_pool logs the driver's reason on the `psycopg.pool` logger. If you need other timeouts, pass your own pool. A malformed connection string raises `PostgresStorageError("Invalid connection string.")` and never repeats libpq's text, which can contain the password.

After a database restart or failover, each stale connection in the owned pool fails one call before the pool replaces it. Behind PgBouncer in transaction mode older than 1.21, or without `max_prepared_statements`, psycopg's prepared statements fail, because psycopg prepares a query after its fifth run. For either case, pass your own pool. `check=AsyncConnectionPool.check_connection` tests each connection as the pool hands it out, at the cost of one round trip, and `prepare_threshold=None` turns prepared statements off.

```python
import asyncio
import sys

from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    pool = AsyncConnectionPool(
        DSN,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=AsyncConnectionPool.check_connection,
    )
    async with pool, PostgresPersistence(application_id="my-app", client=pool) as hub:
        await hub.migrate()
        print(await hub.pending_migrations())


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

## Microsoft Entra ID on Azure

With the `azure` extra, pass an `azure-identity` credential as `credential=` and the owned pool signs in to Azure Database for PostgreSQL with a Microsoft Entra ID token for each new connection, instead of a password:

```python
from azure.identity.aio import DefaultAzureCredential

DSN = "host=my-server.postgres.database.azure.com dbname=agent_framework user=ada@contoso.com"


async def main() -> None:
    async with (
        DefaultAzureCredential() as credential,
        PostgresPersistence(application_id="my-app", connection_string=DSN, credential=credential) as hub,
    ):
        await hub.migrate()
```

A failing credential raises `PostgresStorageError` naming its error on the first call. A managed identity or a service principal has no user name in its token, so set `user=` to its database role.

This is verified with a fake credential against PostgreSQL 16 and 17 and has not yet been run against an Azure server. [docs/azure-entra.md](docs/azure-entra.md) covers the server setup, managed identities, token lifetime and the CLI.

## Schema

Tables are created in `schema` and named `{table_prefix}{name}`. With the defaults:

| Table | Key | Holds |
|---|---|---|
| `af_history_messages` | `id`, read by application, tenant, agent, source and session | One `Message.to_dict()` per row |
| `af_sessions` | `application_id, session_id` | `AgentSession.to_dict()` |
| `af_checkpoints` | `application_id, scope, checkpoint_id` | The encoded `WorkflowCheckpoint` |
| `af_thread_snapshots` | `application_id, scope, thread_id` | The latest AG-UI thread snapshot |
| `af_documents` | `application_id, collection, scope, key` | Payload, metadata and revision |
| `af_leases` | `application_id, resource` | Lease owner, token and expiry |
| `af_migrations` | `version` | Applied migration versions |

## Retention

Retention is off unless you pass `RetentionPolicy(ttl=...)`. With a TTL, every write sets `expires_at` to now plus the TTL, and `purge()` or the `purge` CLI command tombstones or deletes the rows whose `expires_at` has passed. History messages and checkpoints are always deleted. See [docs/retention.md](docs/retention.md).

## Private Agent Framework imports

The package uses three names MAF does not export. `filter_new_messages` deduplicates history, and `encode_checkpoint_value` and `decode_checkpoint_value` encode checkpoints. Microsoft's own Cosmos DB and Redis packages import the same names. `_framework.py` is the only module that imports them, and `tests/unit/test_framework.py` fails when one of them moves. CI also runs the suite against the newest `agent-framework-core` the declared range allows, and a weekly canary runs it against the newest release past the upper bound, so an upstream rename shows up there before a patch release widens the bound. See [docs/compatibility.md](docs/compatibility.md).

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
- [docs/azure-entra.md](docs/azure-entra.md) covers Microsoft Entra ID sign-in on Azure Database for PostgreSQL.
- [docs/compatibility.md](docs/compatibility.md) covers versions, private imports, JSONB limits and Windows.

Runnable scripts for each store are in `samples/`.

## License

MIT. See `LICENSE`.
