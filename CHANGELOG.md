# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-04

The first release on PyPI.

### Added

- Microsoft Entra ID sign-in for Azure Database for PostgreSQL: `credential=` on every store and on `PostgresPersistence` takes a sync or async `azure-identity` credential, and the owned pool uses a fresh token as the password of each new connection. A failing credential raises `PostgresStorageError` naming its error on the first call. `EntraCredential` and `ENTRA_SCOPE` are exported. See `docs/azure-entra.md`.
- The `azure` extra, which installs `azure-identity` and `aiohttp`, the async transport the `azure.identity.aio` credentials need.
- `PostgresStorageError.sqlstate` and `.retryable`.
- `pool_kwargs` on every store and the hub.
- `UntestedAgentFrameworkWarning`, emitted once at import when the installed `agent-framework-core` or `agent-framework-ag-ui` is a newer minor than this release was tested on. See `docs/compatibility.md` for how to silence it.

### Changed

- The document store is gone before the first release: `PostgresDocumentStore`, `Document`, `DocumentSummary`, `RevisionConflict` and `PostgresPersistence.document_store()` are removed, and schema version 1 creates no `af_documents` table. No MAF abstraction maps to them. `PostgresLeases` stays, as a cross-replica lock.
- Requires `agent-framework-core>=1.20.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.5.0,<2`. On MAF 1.19, history dedupe drops any new message that matches one already stored, so a repeated "yes" was lost. 1.20 fixed that. Tested on `agent-framework-core` 1.20.0 and `agent-framework-ag-ui` 1.5.0.
- Owned pools set TCP keepalives and `tcp_user_timeout` unless the connection string does.
- With `credential=`, `sslmode` defaults to `verify-full` with the system trust store, and `disable`, `allow` and `prefer` are refused.

### Fixed

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
