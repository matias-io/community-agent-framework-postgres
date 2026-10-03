# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-02

0.1.0 was never published, so 0.1.1 is the first release on PyPI.

### Added

- Microsoft Entra ID sign-in for Azure Database for PostgreSQL: `credential=` on every store and on `PostgresPersistence` takes a sync or async `azure-identity` credential, and the owned pool uses a fresh token as the password of each new connection. See `docs/azure-entra.md`.
- The `azure` extra, which installs `azure-identity`.

### Changed

- The supported MAF range is now declared as the tested range: `agent-framework-core>=1.19.0,<1.21` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.4.0,<1.6`. Verified on `agent-framework-core` 1.20.0 with `agent-framework-ag-ui` 1.5.0. `docs/compatibility.md` describes the support policy.
- `PostgresHistoryProvider` follows the installed MAF's replay rule, which changed in 1.20. When the stored history is a single user message without an id and the next batch starts with that same message, 1.20 stores the repeat; 1.19 drops it.

### Fixed

- A string with a lone surrogate now raises `ValueError` before any SQL runs, instead of a `UnicodeEncodeError` from inside psycopg.
- `PurgeReport` can be pickled, copied and passed to `dataclasses.asdict`. `counts` is still read-only.

## [0.1.0] - 2026-10-01

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
