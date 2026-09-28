# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `PostgresHistoryProvider`, a MAF `HistoryProvider` with one row per message, scoped by application, tenant, agent, source and session.
- `PostgresSessionStore`, a MAF `SessionStore` for `AgentSession` snapshots.
- `PostgresCheckpointStorage`, a workflow `CheckpointStorage` with MAF's checkpoint encoding and an optional `scope`.
- `PostgresAGUIThreadSnapshotStore`, an `AGUIThreadSnapshotStore` for AG-UI, behind the `ag-ui` extra.
- `PostgresDocumentStore`, JSON documents with revisions, optimistic concurrency and keyset paging.
- `PostgresLeases`, renewable leases judged by the database clock. Each acquisition gets its own token, so a lease has one holder at a time even when two holders share an `owner`, and a crashed holder is recovered by expiry.
- `PostgresPersistence`, a hub that shares one connection pool across every store and runs migrations.
- `RetentionPolicy` with tombstone and delete modes, and `purge()` on every store and on the hub.
- Numbered schema migrations through `migrate()`, serialized across processes by an advisory lock.
- A CLI, `python -m agent_framework_community_postgres`, with `migrate`, `migrate --print`, `status` and `purge`.
- Stores created by a hub refuse to run after the hub closes.
