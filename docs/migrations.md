# Migrations

The schema is a numbered list of migrations, `MIGRATIONS`, where version `n` is entry `n - 1`. Applied versions are recorded in `af_migrations`. Version 1 creates every table in this package.

## From Python

```python
import asyncio
import sys

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        report = await hub.migrate()
        print(report.applied, report.current)
        print(await hub.pending_migrations())


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

`migrate()` returns a `MigrationReport`. `applied` holds the versions this call ran, and `current` the version now in place. On an empty schema the script prints `(1,) 1` and `[]`. A second run prints `() 1`.

## Concurrency

`migrate()` is safe to call from every replica at startup. Each step takes `pg_advisory_xact_lock` on a key derived from the schema and prefix, so two processes never migrate the same tables at once. Each version runs in its own transaction and is skipped when another process recorded it while this one waited. The lock is transaction scoped, so it works behind a transaction-mode pooler and is released when the transaction ends.

SQL applied by hand does not take this lock. Do not run it while an application instance may be calling `migrate()` on the same schema.

## From the command line

```bash
export POSTGRES_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework
python -m agent_framework_community_postgres migrate
python -m agent_framework_community_postgres status
```

`migrate` prints `applied: 1; current version: 1`, and `status` prints `current version: 1; pending: none`. `purge [--mode tombstone|delete] --application-id ID` runs `hub.purge()`. See [retention.md](retention.md). `--connection-string`, `--schema`, `--table-prefix` and `--application-id` may come before or after the subcommand. An error prints one `error:` line to stderr and exits with status 1. An invalid option, such as an uppercase `--schema`, a `--table-prefix` that is too long or an empty `--connection-string`, exits with status 2, and `migrate --print` checks `--schema` and `--table-prefix` too. The connection string is never printed. When the database refuses the connection, psycopg_pool's log lines with the driver's reason come before that line.

## SQL for a DBA

`migrate --print` writes the SQL instead of running it and needs no database. Each version is wrapped in `BEGIN;` and `COMMIT;`, so `psql -f` applies a version atomically. It prints every version, so apply it to a schema that has none of them. The same text is available from Python as `render_migrations(TableNames(schema=..., prefix=...))`.

```bash
python -m agent_framework_community_postgres migrate --print > schema.sql
psql "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework" -v ON_ERROR_STOP=1 -f schema.sql
```

For version 1 with the defaults it prints:

```sql
CREATE TABLE IF NOT EXISTS "public"."af_migrations" ( version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
-- version 1
BEGIN;
CREATE TABLE "public"."af_history_messages" ( id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, application_id text NOT NULL, tenant_id text NOT NULL DEFAULT '', agent_id text NOT NULL DEFAULT '', source_id text NOT NULL, session_id text NOT NULL, message jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz);
CREATE INDEX "af_history_messages_session_idx" ON "public"."af_history_messages" (application_id, tenant_id, agent_id, source_id, session_id, id);
CREATE INDEX "af_history_messages_expires_idx" ON "public"."af_history_messages" (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE "public"."af_sessions" ( application_id text NOT NULL, session_id text NOT NULL, snapshot jsonb, snapshot_version text NOT NULL, revision bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, purged_at timestamptz, PRIMARY KEY (application_id, session_id));
CREATE TABLE "public"."af_checkpoints" ( application_id text NOT NULL, scope text NOT NULL DEFAULT '', workflow_name text NOT NULL, checkpoint_id text NOT NULL, previous_checkpoint_id text, checkpoint_timestamp timestamptz NOT NULL, iteration_count integer NOT NULL, encoded jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, PRIMARY KEY (application_id, scope, checkpoint_id));
CREATE INDEX "af_checkpoints_workflow_idx" ON "public"."af_checkpoints" (application_id, scope, workflow_name, checkpoint_timestamp, created_at);
CREATE TABLE "public"."af_thread_snapshots" ( application_id text NOT NULL, scope text NOT NULL, thread_id text NOT NULL, messages jsonb, state jsonb, interrupt jsonb, session_state jsonb, revision bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, purged_at timestamptz, PRIMARY KEY (application_id, scope, thread_id));
CREATE TABLE "public"."af_documents" ( application_id text NOT NULL, collection text NOT NULL, scope text NOT NULL, key text NOT NULL, payload jsonb, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, revision bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, purged_at timestamptz, PRIMARY KEY (application_id, collection, scope, key));
CREATE INDEX "af_documents_scope_idx" ON "public"."af_documents" (application_id, collection, scope, updated_at DESC);
CREATE TABLE "public"."af_leases" ( application_id text NOT NULL, resource text NOT NULL, owner text NOT NULL, token text NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY (application_id, resource));
INSERT INTO "public"."af_migrations" (version) VALUES (1);
COMMIT;
```

An application that cannot run DDL can skip `migrate()` and call `hub.pending_migrations()` at startup. It returns the versions still missing and only reads.

## Retention

Migrations hold no application data. `af_migrations` is never purged.
