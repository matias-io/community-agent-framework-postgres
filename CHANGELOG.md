# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-04

The first release on PyPI.

### Added

- Microsoft Entra ID sign-in for Azure Database for PostgreSQL: `credential=` on every store and on `PostgresPersistence` takes a sync or async `azure-identity` credential, and the owned pool uses a fresh token as the password of each new connection. A failing credential raises `PostgresStorageError` naming its error on the first call. With a credential, `sslmode` defaults to `verify-full` with the system trust store, and `disable`, `allow` and `prefer` are refused. `EntraCredential` and `ENTRA_SCOPE` are exported. See `docs/azure-entra.md`.
- The `azure` extra, which installs `azure-identity` and `aiohttp`, the async transport the `azure.identity.aio` credentials need.
- `PostgresStorageError.sqlstate` and `.retryable`.
- `pool_kwargs` on every store and the hub.
- `PostgresLeases.purge()`, which deletes the expired leases of its application and returns a `PurgeReport`.
- `compatibility()` and `Compatibility`: the installed `agent-framework-core` and `agent-framework-ag-ui` next to the tested minors. The first store or hub built in a process logs one warning per untested distribution: a newer minor than the tested ones, or a new major, which the warning calls outside the declared range. Importing the package emits no warning and logs nothing.
- `migrate(lock_timeout=)`, `PostgresPersistence.current_version()`, `migrate --print --from-version N`.
- `tenant_id` on `PostgresCheckpointStorage` and `PostgresPersistence.checkpoint_storage()`, part of the checkpoint row key next to `scope`.
- `PostgresHistoryProvider.list_sessions(limit=, after=)` pages session ids by keyset: `after` starts a page after the given id.

### Changed

- Requires `agent-framework-core>=1.20.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.5.0,<2`. On MAF 1.19, history dedupe drops any new message that matches one already stored, so a repeated "yes" was lost. 1.20 fixed that. Tested on `agent-framework-core` 1.20.0 and `agent-framework-ag-ui` 1.5.0.
- Owned pools set TCP keepalives and `tcp_user_timeout` unless the connection string or `pool_kwargs` sets them.
- The migration advisory lock key is now `hashtextextended` over the schema and table prefix; it was a crc32 of the schema-qualified migrations table name.
- Purge runs in batches of 1000 rows, one transaction per batch, and the hub purge also removes expired leases.
- Schema version 1 is the first released schema: checkpoints carry `tenant_id`, `payload_format` and a write `sequence`; every purged table has an expiry index; there is no document table. A database created by the unpublished 0.1.0 snapshot must be recreated.
- Checkpoint `get_latest` and listing order by write order within a scope, not by `checkpoint.timestamp`, so a host whose clock runs behind cannot hide the checkpoint it wrote last. Saves in one scope take turns on an advisory lock, and re-saving an id replaces its content and keeps its position.
- Checkpoint `load` and `get_latest` raise `WorkflowCheckpointException` when the row fails to decode, instead of `get_latest` falling back to an older checkpoint. `list_checkpoints` still skips such rows with a warning.
- `list_checkpoint_ids` reads ids without decoding, so it returns every id, including one whose row fails to decode.
- `PostgresSessionStore.get` raises `ValueError` when the stored snapshot has an unsupported `snapshot_version` or is not a JSON object, as MAF's `FileSessionStore` does for an unreadable file, instead of returning `None` as if the session were absent. A purged session still returns `None`.
- `PostgresAGUIThreadSnapshotStore.clear` follows the AG-UI protocol signature, `clear(*, scope=None)`: no scope removes every snapshot of the application, a scope removes that scope's. An empty scope raises `ValueError`.
- History saves and `clear` take the session's advisory lock inside a `READ COMMITTED` transaction, so concurrent saves no longer duplicate history on a server whose default isolation level is `REPEATABLE READ`. The lock key now includes the schema and table prefix, and `clear` no longer runs in the middle of a save.
- A history save reads only the newest stored messages, as many as it was given, to deduplicate, instead of the whole session. The result is the same under MAF 1.20's `filter_new_messages`, and a save no longer slows down as a session grows.
- A history save raises `PostgresStorageError` when a stored message it needs for deduplication no longer deserializes, instead of skipping it and possibly storing a replay twice. `get_messages` still skips such rows with a warning.
- History retention keeps a session whole: a purge deletes a session's messages only once all of them have expired, instead of deleting an active session's oldest messages.

### Removed

- The document store is gone before the first release: `PostgresDocumentStore`, `Document`, `DocumentSummary`, `RevisionConflict` and `PostgresPersistence.document_store()` are removed, and schema version 1 creates no `af_documents` table. No MAF abstraction maps to them. `PostgresLeases` stays, as a cross-replica lock.
- The CLI's `--connection-string`; set `POSTGRES_CONNECTION_STRING`, which keeps the password out of the command line that `ps` shows and out of shell history.

### Fixed

- A lease's release after a failed or cancelled block waits at most 5 seconds, so a slow database cannot hold a cancelling task. The release keeps running in the background, and an unreleased lease frees itself on expiry.
- Custom credential exception text stays out of package errors and logs. Azure SDK exception diagnostics remain available, and the original exception remains the chained cause.
- Hub factories now reject `schema` and `table_prefix` overrides, so every store they create stays within the namespace covered by `hub.migrate()` and `hub.purge()`. Per-store `retention` overrides remain supported.
- A string with a lone surrogate now raises `ValueError` before any SQL runs, instead of a `UnicodeEncodeError` from inside psycopg.
- `PurgeReport` can be pickled, copied and passed to `dataclasses.asdict`, and its `counts` is read-only: it is a mapping, not a `dict`, so no `dict` method can change it.

## [0.1.0] - 2026-10-01

Never published to PyPI. It declared `agent-framework-core>=1.19.0,<2`.

### Added

- `PostgresHistoryProvider`, a MAF `HistoryProvider` with one row per message, scoped by application, tenant, agent, source and session.
- `PostgresSessionStore`, a MAF `SessionStore` for `AgentSession` snapshots.
- `PostgresCheckpointStorage`, a workflow `CheckpointStorage` with MAF's checkpoint encoding and an optional `scope`.
- `PostgresAGUIThreadSnapshotStore`, an `AGUIThreadSnapshotStore` for AG-UI, behind the `ag-ui` extra.
- `PostgresDocumentStore`, JSON documents with revisions, optimistic concurrency and keyset paging.
- `PostgresLeases`, renewable leases judged by the database clock. Each acquisition gets its own token, so a lease has one holder at a time even when two holders share an `owner`, and a crashed holder is recovered by expiry.
- `PostgresPersistence`, a hub that shares one connection pool across every store and runs migrations.
- `RetentionPolicy` with tombstone and delete modes, and `purge()` on every store and on the hub. A purge removes every row whose stored `expires_at` has passed, whichever policy stamped it, so the hub and the CLI need no TTL.
- Numbered schema migrations through `migrate()`, serialized across processes by an advisory lock.
- A CLI, `python -m agent_framework_community_postgres`, with `migrate`, `migrate --print`, `status` and `purge [--mode tombstone|delete] --application-id ID`.
- Stores created by a hub refuse to run after the hub closes.

[0.1.1]: https://github.com/matias-io/community-agent-framework-postgres/compare/50c91c5...v0.1.1
[0.1.0]: https://github.com/matias-io/community-agent-framework-postgres/tree/50c91c5
