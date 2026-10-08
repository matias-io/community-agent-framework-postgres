# Migrations

The schema is a numbered list of migrations, `MIGRATIONS`, where version `n` is entry `n - 1`. Applied versions are recorded in `af_migrations`. Version 1 creates every table in this package. The schema must already exist, because `migrate()` creates tables but not the schema. Run `CREATE SCHEMA` first for anything other than `public`.

0.1.1 is the first release, and its version 1 is the first released schema. The unpublished 0.1.0 snapshot created a different version 1, and there is no upgrade path from it: drop the tables it created, or the database, and run `migrate()` again.

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
        print(await hub.pending_migrations(), await hub.current_version())


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

`migrate()` returns a `MigrationReport`. `applied` holds the versions this call ran, and `current` the version now in place. On an empty schema the script prints `(1,) 1` and `[] 1`. A second run prints `() 1`. `current_version()` returns 0 before the first migration.

A database migrated by a newer release of this package has a version this one does not know. `migrate()` and `pending_migrations()` then raise `PostgresStorageError` and ask you to upgrade the package, and the CLI's `status` prints the real version before that error. `render_migrations` raises `ValueError` for a version outside 1 to `len(MIGRATIONS)`.

## Lock timeout

DDL such as `ALTER TABLE` needs a lock that waits for every open transaction on the table, and every query that arrives after it waits behind it. So each version runs with `SET LOCAL lock_timeout`, 30 seconds by default, set inside that version's transaction once the migration lock is held. A version that cannot get its locks in time is rolled back and `migrate()` raises a `PostgresStorageError` with `sqlstate` `55P03` and `retryable` true. Versions applied before it stay applied, so calling `migrate()` again resumes from there.

```python
from datetime import timedelta

report = await hub.migrate(lock_timeout=timedelta(seconds=5))
```

`lock_timeout` must be at least one millisecond, because PostgreSQL reads 0 as no limit. The timeout covers only the migration's own statements. A replica waiting for another replica to finish migrating waits on the advisory lock with no limit.

## Concurrency

`migrate()` is safe to call from every replica at startup. Each step takes `pg_advisory_xact_lock` on a key derived from the schema and prefix, so two processes never migrate the same tables at once. Each version runs in its own transaction and is skipped when another process recorded it while this one waited. The lock is transaction scoped, so it is released when the transaction ends, and a transaction-mode pooler keeps it on one server connection. Behind PgBouncer in transaction mode, pass a pool with `prepare_threshold=None`. See [Standalone use](../README.md#standalone-use).

SQL applied by hand does not take this lock. Do not run it while an application instance may be calling `migrate()` on the same schema.

## From the command line

```bash
export POSTGRES_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework
python -m agent_framework_community_postgres migrate
python -m agent_framework_community_postgres status
```

The CLI reads the connection string only from `POSTGRES_CONNECTION_STRING`, which keeps the password out of the command line that `ps` shows and out of shell history. `migrate` prints `applied: 1; current version: 1`, and `status` prints `current version: 1; pending: none`. `purge [--mode tombstone|delete] --application-id ID` runs `hub.purge()`. See [retention.md](retention.md). `--schema`, `--table-prefix` and `--application-id` may come before or after the subcommand. An error prints one `error:` line to stderr and exits with status 1. An invalid option, such as an uppercase `--schema` or a `--table-prefix` that is too long, exits with status 2, and `migrate --print` checks `--schema` and `--table-prefix` too. The connection string is never printed. When the database refuses the connection, psycopg_pool's log lines with the driver's reason come before that line.

## SQL for a DBA

`migrate --print` writes the SQL instead of running it and needs no database. Each version is wrapped in `BEGIN;` and `COMMIT;` and sets the default 30 second `lock_timeout`, so `psql -f` applies a version atomically and gives up instead of queueing behind application traffic. With no other option it prints every version, so apply it to a schema that has none of them. `--from-version N` prints only the versions after `N`, for a database `status` reports at version `N`; when there is none it prints nothing. The same text is available from Python as `render_migrations(TableNames(schema=..., prefix=...))`.

```bash
python -m agent_framework_community_postgres migrate --print > schema.sql
psql "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework" -v ON_ERROR_STOP=1 -f schema.sql
python -m agent_framework_community_postgres migrate --print --from-version 1 > upgrade.sql
```

For version 1 with the defaults it prints:

```sql
CREATE TABLE IF NOT EXISTS "public"."af_migrations" ( version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
-- version 1
BEGIN;
SET LOCAL lock_timeout = '30000ms';
CREATE TABLE "public"."af_history_messages" ( id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, application_id text NOT NULL, tenant_id text NOT NULL DEFAULT '', agent_id text NOT NULL DEFAULT '', source_id text NOT NULL, session_id text NOT NULL, message jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz);
CREATE INDEX "af_history_messages_session_idx" ON "public"."af_history_messages" (application_id, tenant_id, agent_id, source_id, session_id, id);
CREATE INDEX "af_history_messages_expires_idx" ON "public"."af_history_messages" (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE "public"."af_sessions" ( application_id text NOT NULL, session_id text NOT NULL, snapshot jsonb, snapshot_version text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, purged_at timestamptz, PRIMARY KEY (application_id, session_id));
CREATE INDEX "af_sessions_expires_idx" ON "public"."af_sessions" (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE "public"."af_checkpoints" ( application_id text NOT NULL, tenant_id text NOT NULL DEFAULT '', scope text NOT NULL DEFAULT '', payload_format text NOT NULL DEFAULT 'agent-framework.python.checkpoint.v1', workflow_name text NOT NULL, checkpoint_id text NOT NULL, previous_checkpoint_id text, checkpoint_timestamp timestamptz NOT NULL, sequence bigint GENERATED ALWAYS AS IDENTITY, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, PRIMARY KEY (application_id, tenant_id, scope, checkpoint_id));
CREATE INDEX "af_checkpoints_scope_idx" ON "public"."af_checkpoints" (application_id, tenant_id, scope, workflow_name, sequence);
CREATE INDEX "af_checkpoints_expires_idx" ON "public"."af_checkpoints" (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE "public"."af_thread_snapshots" ( application_id text NOT NULL, scope text NOT NULL, thread_id text NOT NULL, messages jsonb, state jsonb, interrupt jsonb, session_state jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, purged_at timestamptz, PRIMARY KEY (application_id, scope, thread_id));
CREATE INDEX "af_thread_snapshots_expires_idx" ON "public"."af_thread_snapshots" (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE "public"."af_leases" ( application_id text NOT NULL, resource text NOT NULL, owner text NOT NULL, token text NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY (application_id, resource));
CREATE INDEX "af_leases_expires_idx" ON "public"."af_leases" (expires_at);
INSERT INTO "public"."af_migrations" (version) VALUES (1);
COMMIT;
```

An application that cannot run DDL can skip `migrate()` and call `hub.pending_migrations()` at startup. It returns the versions still missing and only reads.

## Schema version 1

| Table | Columns | Primary key | Indexes |
|---|---|---|---|
| `af_history_messages` | `id`, `application_id`, `tenant_id`, `agent_id`, `source_id`, `session_id`, `message`, `created_at`, `expires_at` | `id` | `(application_id, tenant_id, agent_id, source_id, session_id, id)`; `expires_at` where set |
| `af_sessions` | `application_id`, `session_id`, `snapshot`, `snapshot_version`, `created_at`, `updated_at`, `expires_at`, `purged_at` | `application_id, session_id` | `expires_at` where set |
| `af_checkpoints` | `application_id`, `tenant_id`, `scope`, `payload_format`, `workflow_name`, `checkpoint_id`, `previous_checkpoint_id`, `checkpoint_timestamp`, `sequence`, `payload`, `created_at`, `expires_at` | `application_id, tenant_id, scope, checkpoint_id` | `(application_id, tenant_id, scope, workflow_name, sequence)`; `expires_at` where set |
| `af_thread_snapshots` | `application_id`, `scope`, `thread_id`, `messages`, `state`, `interrupt`, `session_state`, `created_at`, `updated_at`, `expires_at`, `purged_at` | `application_id, scope, thread_id` | `expires_at` where set |
| `af_leases` | `application_id`, `resource`, `owner`, `token`, `expires_at` | `application_id, resource` | `expires_at` |
| `af_migrations` | `version`, `applied_at` | `version` | none |

On `af_checkpoints`, `tenant_id` and `scope` default to the empty string, `payload_format` names the encoding of `payload`, and `sequence` records write order.

## Roles

`migrate()` and the CLI's `migrate` need `CREATE` on the schema, and they own the tables they create. The stores need less: `USAGE` on the schema and `SELECT, INSERT, UPDATE, DELETE` on the package's tables, plus `SELECT` on `af_migrations` for `pending_migrations()` and `current_version()`. To run the application as a role that cannot change the schema, migrate as an owner role and grant the runtime role only that:

```sql
GRANT USAGE ON SCHEMA agents TO app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON agents.af_history_messages, agents.af_sessions, agents.af_checkpoints,
     agents.af_thread_snapshots, agents.af_leases
  TO app_runtime;
GRANT SELECT ON agents.af_migrations TO app_runtime;
```

## Retention

Migrations hold no application data. `af_migrations` is never purged.
