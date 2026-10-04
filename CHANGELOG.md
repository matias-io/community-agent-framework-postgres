# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-02

### Added

- Microsoft Entra ID sign-in for Azure Database for PostgreSQL: `credential=` on every store and on `PostgresPersistence` takes a sync or async `azure-identity` credential, and the owned pool uses a fresh token as the password of each new connection. A failing credential raises `PostgresStorageError` naming its error on the first call. `EntraCredential` and `ENTRA_SCOPE` are exported. See `docs/azure-entra.md`.
- The `azure` extra, which installs `azure-identity` and `aiohttp`, the async transport the `azure.identity.aio` credentials need.
- `UntestedAgentFrameworkWarning`, emitted once at import when the installed `agent-framework-core` or `agent-framework-ag-ui` is a newer minor than this release was tested on. See `docs/compatibility.md` for how to silence it.

### Changed

- Declared MAF range: `agent-framework-core>=1.19.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.4.0,<2`. Tested on `agent-framework-core` 1.19 and 1.20 and on `agent-framework-ag-ui` 1.4 and 1.5.

### Fixed

- A string with a lone surrogate now raises `ValueError` before any SQL runs, instead of a `UnicodeEncodeError` from inside psycopg.
- `PurgeReport` can be pickled, copied and passed to `dataclasses.asdict`, and its `counts` is read-only: it is a mapping, not a `dict`, so no `dict` method can change it.

### Notes

- 0.1.1 is the first release on PyPI. 0.1.0 is commit `97d3e3a` on `main`, never tagged on GitHub and never published; 0.1.1 supersedes it.
- MAF 1.20 changed its own replay rule, and `PostgresHistoryProvider` follows the installed MAF. When the stored history is a single user message without an id and the next batch starts with that same message, 1.20 stores the repeat; 1.19 drops it.

## [0.1.0] - 2026-10-01

Commit `97d3e3a` on `main`, never tagged on GitHub and not published to PyPI.

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

[0.1.1]: https://github.com/matias-io/community-agent-framework-postgres/compare/97d3e3a...v0.1.1
[0.1.0]: https://github.com/matias-io/community-agent-framework-postgres/tree/97d3e3a
