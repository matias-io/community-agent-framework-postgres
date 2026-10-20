# community-agent-framework-postgres

PostgreSQL storage for [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (MAF) in Python: chat history, `AgentSession` snapshots, workflow checkpoints and AG-UI thread snapshots in plain PostgreSQL tables, with no vector extension and no embeddings.

Each class implements a MAF interface, so an agent or workflow uses it the way it uses MAF's in-memory or file stores. `PostgresPersistence` builds every store over one connection pool and runs the schema migrations. This is a community package by Matias Suxo Salinas. Microsoft does not maintain or support it.

## Status

Alpha. Public names and constructor arguments may change in a 0.x minor release, and the [changelog](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/CHANGELOG.md) lists every such change. Schema changes ship as new numbered migrations, so `migrate()` upgrades an existing database in place.

## What it implements

| Class | MAF interface | Extra | Notes |
|---|---|---|---|
| `PostgresHistoryProvider` | `HistoryProvider` | none | One row per message. Drops replayed messages with MAF's own `filter_new_messages` |
| `PostgresSessionStore` | `SessionStore` | none | MAF marks `SessionStore` experimental |
| `PostgresCheckpointStorage` | `CheckpointStorage` | none | MAF's checkpoint encoding; decodes only MAF's safe types unless `allowed_checkpoint_types` adds more |
| `PostgresAGUIThreadSnapshotStore` | `AGUIThreadSnapshotStore` | `ag-ui` | The AG-UI endpoint needs a `snapshot_scope_resolver` next to `snapshot_store` |
| `PostgresPersistence` | none | none | Builds the stores above over one pool, migrates and purges |
| `PostgresLeases` | none, an addition | none | A lock across replicas; MAF hosting serializes runs within one process only |

Each store scopes its rows the way its closest Microsoft counterpart does. The history provider takes `application_id` plus optional `tenant_id` and `agent_id`, as `RedisHistoryProvider` and `VectorStoreHistoryProvider` do. The session store keys by session id, as `SessionStore` does. The AG-UI store uses the protocol's `scope`. Checkpoints take an optional `tenant_id` and a `scope` naming one run.

## How it compares with Microsoft's packages

As of agent-framework-core 1.22 and October 2026:

| You need | Microsoft today | This package |
|---|---|---|
| Chat history in PostgreSQL | `VectorStoreHistoryProvider` (experimental) over `PostgresStore` from `agent-framework-postgres`, a pgvector connector, which can also search history by meaning | `PostgresHistoryProvider`: one row per message in insert order, saves to a session serialized across processes, `max_messages` and TTL retention |
| Workflow checkpoints | Cosmos DB, local files or Foundry hosting. A PostgreSQL store is proposed in [microsoft/agent-framework#8664](https://github.com/microsoft/agent-framework/pull/8664) | `PostgresCheckpointStorage` |
| `AgentSession` across processes | `FileSessionStore` | `PostgresSessionStore` |
| AG-UI threads across restarts | `InMemoryAGUIThreadSnapshotStore` | `PostgresAGUIThreadSnapshotStore` |
| Vector search and embeddings | `agent-framework-postgres` | Not provided. Use Microsoft's package next to this one |

## Install

```bash
uv add community-agent-framework-postgres
uv add "community-agent-framework-postgres[ag-ui]"  # adds PostgresAGUIThreadSnapshotStore
uv add "community-agent-framework-postgres[azure]"  # adds Microsoft Entra ID sign-in
pip install community-agent-framework-postgres
pip install "community-agent-framework-postgres[ag-ui]"
pip install "community-agent-framework-postgres[azure]"
```

The `ag-ui` extra installs `agent-framework-ag-ui`. Without it, every store except `PostgresAGUIThreadSnapshotStore` works. `from agent_framework_community_postgres import *` also needs the extra, because `__all__` lists that store. The `azure` extra installs `azure-identity`, and `aiohttp` for its async credentials.

On Windows, async psycopg cannot run on the default `ProactorEventLoop`, so the examples below start on a selector loop. Run uvicorn 0.36 or later with `--loop asyncio:SelectorEventLoop`. [docs/compatibility.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/compatibility.md) has the details.

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


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Every store the hub creates shares its pool, `application_id`, `schema` and `table_prefix`. Its factories take the same arguments as the store classes except those and the other connection arguments, and raise `TypeError` if you pass `application_id`, `client`, `credential`, `pool_kwargs`, `schema`, `table_prefix`, `connection_string`, `env_file_path` or `env_file_encoding`. For another schema or prefix, create a separate hub or a standalone store. `retention` defaults to the hub's policy and can be set per store; `retention=None` gives a store whose rows never expire. `hub.purge()` covers every store created by that hub and needs no policy of its own. After `hub.close()`, every store it created raises `PostgresStorageError`.

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

Pass the checkpoint storage to `WorkflowBuilder`, and a workflow that dies part way through can finish in a new process. [samples/resume_after_restart.py](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/samples/resume_after_restart.py) kills a two-step workflow after its first step. Run it again with `resume` and a fresh process loads the latest checkpoint and runs the second step:

```python
storage = hub.checkpoint_storage(scope="order-42")
workflow = build(storage, crash=False)  # the same graph, built fresh in the new process
latest = await storage.get_latest(workflow_name=workflow.name)
if latest is not None:
    result = await workflow.run(checkpoint_id=latest.checkpoint_id)
```

Give each run its own `scope`, such as a conversation or order id. Saves within one scope take turns, which is how "latest" stays the last checkpoint written. [docs/checkpoints.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/checkpoints.md) covers the rest.

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

In an agent, create the new session with `agent.create_session(session_id=...)` instead of `AgentSession(...)`. See [docs/history.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/history.md) and [docs/sessions.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/sessions.md).

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

Every store and the hub take `connection_string` or `client`. Without either, the connection string comes from `POSTGRES_CONNECTION_STRING` through MAF's `load_settings`, the way Microsoft's `agent-framework-postgres` reads it. A connection string gives the store its own pool. A psycopg `AsyncConnection` or `AsyncConnectionPool` you pass is borrowed and never closed. Errors are `PostgresStorageError` with the driver exception chained, a `sqlstate` and a `retryable` flag. [docs/connections.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/connections.md) covers pool defaults and sizing, timeouts, PgBouncer, borrowed transactions and which errors are retryable.

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

This is verified with a fake credential against PostgreSQL 17 locally, and the CI matrix runs the same tests on PostgreSQL 16 and 17. No real Azure server has been used yet. [docs/azure-entra.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/azure-entra.md) covers the server setup, managed identities, token lifetime and the CLI.

## Schema

Tables are created in `schema` and named `{table_prefix}{name}`. With the defaults:

| Table | Key | Holds |
|---|---|---|
| `af_history_messages` | `id`, read by application, tenant, agent, source and session | One `Message.to_dict()` per row |
| `af_sessions` | `application_id, session_id` | `AgentSession.to_dict()` |
| `af_checkpoints` | `application_id, tenant_id, scope, checkpoint_id` | The encoded `WorkflowCheckpoint` |
| `af_thread_snapshots` | `application_id, scope, thread_id` | The latest AG-UI thread snapshot |
| `af_leases` | `application_id, resource` | Lease owner, token and expiry |
| `af_migrations` | `version` | Applied migration versions |

## Retention

Retention is off unless you pass `RetentionPolicy(ttl=...)`. With a TTL, every write sets `expires_at` to now plus the TTL, and `purge()` or the `purge` CLI command tombstones or deletes the rows whose `expires_at` has passed. History messages, checkpoints and expired leases are always deleted. Leases take no policy. A history session is purged whole, once every one of its messages has expired, so an active conversation keeps its transcript. A purge works in batches of 1000 rows, one transaction per batch, and never waits on another purge. History is purged a session at a time, and a session that is being saved is left for the next purge. `hub.purge()` covers every table, leases included. It returns a `PurgeReport`; `report.counts` is a read-only mapping, so use `dict(report.counts)` for a copy you can change. Until a purge runs, reads still return expired rows. See [docs/retention.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/retention.md).

## Compatibility

| 0.1.2 | Declared | Tested |
|---|---|---|
| `agent-framework-core` | `>=1.20.0,<2` | 1.20.0, 1.21.0, 1.22.0 |
| `agent-framework-ag-ui` (`ag-ui` extra) | `>=1.5.0,<2` | 1.5.0, 1.5.1, 1.6.0 |
| Python | `>=3.11` | 3.11, 3.12, 3.13, 3.14, 3.15 |
| PostgreSQL | | 16, 17 |
| psycopg | `>=3.3.5,<4` | 3.3.6 |

pip and uv refuse a MAF version outside the declared range. A newer minor inside it installs, and the first store or hub built in a process logs one warning naming the untested version. MAF keeps breaking changes for major versions, so a newer minor is expected to work. MAF 1.19 and older are not supported: their history dedupe drops a message that repeats an earlier one, so a second "yes" is lost. [docs/compatibility.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/compatibility.md) explains the support policy and the check.

## Private Agent Framework imports

The package uses three names MAF does not export. `filter_new_messages` deduplicates history, and `encode_checkpoint_value` and `decode_checkpoint_value` encode checkpoints. Microsoft's own Cosmos DB package imports all three, and its Redis package imports `filter_new_messages`. `_framework.py` is the only module that imports them, and `tests/unit/test_framework.py` fails when one of them moves. CI also runs the suite against the newest `agent-framework-core` the declared range allows, and a weekly canary runs it against the newest MAF release, so an upstream rename shows up there first. See [docs/compatibility.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/compatibility.md).

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

Without `POSTGRES_TEST_CONNECTION_STRING`, the integration and conformance tests are skipped, or fail when the `CI` environment variable is set. See [CONTRIBUTING.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/CONTRIBUTING.md).

## Documentation

- [docs/history.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/history.md) covers `PostgresHistoryProvider`.
- [docs/sessions.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/sessions.md) covers `PostgresSessionStore`.
- [docs/checkpoints.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/checkpoints.md) covers `PostgresCheckpointStorage`.
- [docs/thread-snapshots.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/thread-snapshots.md) covers `PostgresAGUIThreadSnapshotStore`.
- [docs/leases.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/leases.md) covers `PostgresLeases`.
- [docs/retention.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/retention.md) covers `RetentionPolicy` and purging.
- [docs/migrations.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/migrations.md) covers `migrate()`, the CLI, roles and SQL for a DBA.
- [docs/connections.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/connections.md) covers connection arguments, pools, timeouts and errors.
- [docs/azure-entra.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/azure-entra.md) covers Microsoft Entra ID sign-in on Azure Database for PostgreSQL.
- [docs/compatibility.md](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/docs/compatibility.md) covers versions, private imports, JSONB limits and Windows.

Runnable scripts for each store are in [samples/](https://github.com/matias-io/community-agent-framework-postgres/tree/v0.1.2/samples).

## License

MIT. See [LICENSE](https://github.com/matias-io/community-agent-framework-postgres/blob/v0.1.2/LICENSE).
