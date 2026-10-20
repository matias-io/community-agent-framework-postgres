# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-10-19

No public name, schema or migration changes. Upgrading needs no `migrate()` run.

### Added

- Tested on `agent-framework-core` 1.22 and `agent-framework-ag-ui` 1.6, so those versions no longer log the untested-version warning. The full suite was run on each, and CI's latest leg runs the newest release on every change.
- Python 3.15 support. The full suite passed on 3.15.0b4 locally, and the CI matrix runs the 3.15 release. psycopg's binary wheel ships for 3.15.
- On Python 3.15, some dependencies of MAF and of the extras have no wheels yet: PyYAML, which `agent-framework-core` requires, builds from source, and httptools (the `ag-ui` extra, through uvicorn) and frozenlist (the `azure` extra, through aiohttp) need a C compiler.
- `samples/resume_after_restart.py`: a workflow killed after its first step finishes from its checkpoint in a new process. A test runs it, and every other sample that needs only PostgreSQL.
- `docs/connections.md`, which takes over the connection reference from the README and adds pool sizing.
- A warning when a store or hub with a credential uses `sslmode=require` or `verify-ca`, which encrypt without fully verifying the server.

### Changed

- The README leads with what the package implements and how it compares with Microsoft's `agent-framework-postgres`, and links to the guides for detail.
- The hub's factories list their parameters instead of taking `**kwargs`, so editors and type checkers see them. Passing a hub-owned argument still raises `TypeError`, now Python's own "unexpected keyword argument". `connection_string`, `env_file_path` and `env_file_encoding`, which a factory always refused, now raise that `TypeError` instead of `ValueError`. Leaving out `retention` still means the hub's policy, and `retention=None` still means no expiry.
- `PostgresHistoryProvider.list_sessions` reads one index entry per session instead of every message on the page.
- A history save with `max_messages` set trims with one index walk of the kept messages, and deletes nothing while the session is within the limit, instead of an anti-join over the whole session.
- Lease statements read the clock when each statement starts, so a `renew` inside the caller's own transaction extends the lease from the current time.

### Fixed

- A history purge could split a session. It deleted expired messages in 1000-row batches without the session's save lock, so a save landing between or during batches could leave a session with a tool call but not its result. The purge now works a session at a time: it takes each session's save lock, skips a session another writer holds, and deletes the session only if none of its messages is live, checked after the lock. On the benchmark in `docs/retention.md` a full purge took about 2.0 s, against 1.5 to 1.8 s before. A purge round locks at most 64 sessions, so it never holds more than a connection's share of the server's lock table.
- On a server whose default isolation level is `REPEATABLE READ`, concurrent writes to one session or AG-UI snapshot, and a re-save of one checkpoint by two writers, failed with a serialization error. These writes, and purges, now run at `READ COMMITTED`, as history saves, leases and migrations already did.
- An error on a borrowed `AsyncConnection` that has closed was marked `retryable`. A retry would reach the same dead connection, so it is no longer retryable.
- `migrate()` on a connection inside the caller's transaction left its 30-second `lock_timeout` in force until that transaction ended. It now puts back the caller's setting after the DDL.
- `max_messages` accepted `inf`, `nan` and `True`. A float then failed every save with a database error, so a misconfigured provider stored nothing. The constructor now raises `ValueError` for anything but `None` or a non-negative `int`.

### Security and CI

- Ruff runs Microsoft agent-framework's rule set, less its Microsoft copyright-notice rule, and pyright treats a stale ignore comment or cast as an error, as Microsoft's does.
- CI type-checks tests and samples with mypy, zuban, pyrefly, ty and pyright in Microsoft's relaxed profile, type-checks the documentation's code blocks, runs the unit tests on Windows, and fails below 85% coverage. Coverage is 98%.
- The locked development environment moves to pydantic 2.14.1, pydantic-core 2.50.1 and aiohttp 3.14.5, which ship Python 3.15 wheels, and adds the type checkers and twine. The two MAF packages stay at their tested floor.
- `docker-compose.yml` publishes the development database on `127.0.0.1` only.
- Workflows check out without persisted credentials. The release job builds without a cache and refuses a tag whose commit is not on `main`. A separate job checks a downloaded copy of the built wheel and source archive with the locked twine and an import, so nothing a check installs can change what is published.
- Dependabot also proposes updates to the locked development dependencies, except the two MAF packages.
- In CI, a missing `POSTGRES_TEST_CONNECTION_STRING` fails the run instead of skipping the database tests.

## [0.1.1] - 2026-10-11

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

- Requires `agent-framework-core>=1.20.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.5.0,<2`. On MAF 1.19, history dedupe drops any new message that matches one already stored, so a repeated "yes" was lost. 1.20 fixed that. Tested on `agent-framework-core` 1.20.0 and 1.21.0, with `agent-framework-ag-ui` 1.5.0 and 1.5.1.
- Owned pools set TCP keepalives and `tcp_user_timeout` unless the connection string or `pool_kwargs` sets them.
- The migration advisory lock key is now `hashtextextended` over the schema and table prefix; it was a crc32 of the schema-qualified migrations table name.
- Purge runs in batches of 1000 rows, one transaction per batch, and the hub purge also removes expired leases.
- Schema version 1 is the first released schema: checkpoints carry `tenant_id`, `payload_format` and a write `sequence`; every purged table has an expiry index; there is no document table. A database created by the unpublished 0.1.0 snapshot must be recreated.
- Checkpoint `get_latest` and listing order by write order within a scope, not by `checkpoint.timestamp`, so a host whose clock runs behind cannot hide the checkpoint it wrote last. Saves in one scope take turns on an advisory lock, and re-saving an id replaces its content and keeps its position.
- Checkpoint `load` and `get_latest` raise `WorkflowCheckpointException` when the row fails to decode, instead of `get_latest` falling back to an older checkpoint. `list_checkpoints` still skips such rows with a warning.
- `list_checkpoint_ids` reads ids without decoding, so it returns every id, including one whose row fails to decode.
- `PostgresSessionStore.get` raises `ValueError` when the stored snapshot has an unsupported `snapshot_version` or is not a JSON object, as MAF's `FileSessionStore` does for an unreadable file, instead of returning `None` as if the session were absent. A purged session still returns `None`.
- `PostgresAGUIThreadSnapshotStore.clear` follows the AG-UI protocol signature, `clear(*, scope=None)`: no scope removes every snapshot of the application, a scope removes that scope's. An empty scope raises `ValueError`.
- History saves and `clear` take the session's advisory lock inside a `READ COMMITTED` transaction, so concurrent saves no longer duplicate history on a server whose default isolation level is `REPEATABLE READ`. The lock key now includes the schema and table prefix, and `clear` no longer runs in the middle of a save. Migrations and lease statements run in `READ COMMITTED` too, so on such a server replicas that start together no longer fail on a table another replica just created, and a contended lease returns no lease instead of raising a serialization failure.
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

[0.1.2]: https://github.com/matias-io/community-agent-framework-postgres/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/matias-io/community-agent-framework-postgres/compare/50c91c5...v0.1.1
[0.1.0]: https://github.com/matias-io/community-agent-framework-postgres/tree/50c91c5
