"""One pool, one schema, every store: the entry point most applications want."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Self

from agent_framework import SecretString
from psycopg import sql

from ._checkpoint_storage import PostgresCheckpointStorage
from ._client import PostgresClient, TableNames, create_client, require_text
from ._entra import EntraCredential
from ._framework import log_untested_once
from ._history_provider import PostgresHistoryProvider, purge_history
from ._leases import PostgresLeases
from ._migrations import LOCK_TIMEOUT, MigrationReport, current_version, migrate, pending_versions
from ._retention import PurgeReport, RetentionMode, RetentionPolicy, purge_rows
from ._session_store import PostgresSessionStore

if TYPE_CHECKING:
    from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore


@dataclass(frozen=True, repr=False)
class _HubPolicy(RetentionPolicy):
    """The factories' default ``retention``: the hub's own policy. ``None`` still means no expiry, as in 0.1.1."""

    def __repr__(self) -> str:
        return "<the hub's retention policy>"


_HUB_POLICY = _HubPolicy()


class PostgresPersistence:
    """Own one connection pool and hand it to every store this package provides.

    Enter it (``async with``) or call ``open()`` to open the pool up front; otherwise the
    pool opens on first use. Stores it creates borrow the pool and never close it, and
    once the hub is closed they refuse to run. With ``credential``, the pool signs in to Azure
    Database for PostgreSQL with Microsoft Entra ID tokens.
    """

    def __init__(
        self,
        *,
        application_id: str,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | None = None,
        credential: EntraCredential | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
        pool_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        log_untested_once()
        self.application_id = require_text(application_id, "application_id")
        self.names = TableNames(schema=schema, prefix=table_prefix)
        self.retention = retention or RetentionPolicy()
        self._client = create_client(
            connection_string,
            client=client,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            credential=credential,
            pool_kwargs=pool_kwargs,
        )

    @property
    def pool(self) -> PostgresClient:
        """The pool or connection every store created here shares."""
        return self._client.client

    async def open(self) -> None:
        """Open the owned pool (a no-op for a borrowed client)."""
        await self._client.open()

    async def close(self) -> None:
        """Close the owned pool."""
        await self._client.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()

    async def migrate(self, *, lock_timeout: timedelta = LOCK_TIMEOUT) -> MigrationReport:
        """Create or upgrade the schema; each version's DDL gives up after ``lock_timeout`` waiting on a lock."""
        return await migrate(self._client, self.names, lock_timeout=lock_timeout)

    async def pending_migrations(self) -> list[int]:
        """Versions ``migrate`` would apply."""
        return await pending_versions(self._client, self.names)

    async def current_version(self) -> int:
        """The schema version the database is at; 0 before the first migration."""
        return await current_version(self._client, self.names)

    def _shared(self, retention: RetentionPolicy | None) -> dict[str, Any]:
        # The hub owns the application id, schema and prefix (its migrations and purge cover them) and the
        # client (its pool and its close), so the factories take none of them; only retention can differ.
        return {
            "application_id": self.application_id,
            "client": self._client,
            "schema": self.names.schema,
            "table_prefix": self.names.prefix,
            "retention": self.retention if retention is _HUB_POLICY else retention,
        }

    def history_provider(
        self,
        source_id: str = PostgresHistoryProvider.DEFAULT_SOURCE_ID,
        *,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        max_messages: int | None = None,
        load_messages: bool = True,
        store_inputs: bool = True,
        store_context_messages: bool = False,
        store_context_from: set[str] | None = None,
        store_outputs: bool = True,
        retention: RetentionPolicy | None = _HUB_POLICY,
    ) -> PostgresHistoryProvider:
        """A history provider on the shared pool; arguments as on the class, ``retention`` defaulting to the hub's.

        Pass ``retention=None`` or ``RetentionPolicy()`` for a store whose rows never expire.
        """
        return PostgresHistoryProvider(
            source_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            max_messages=max_messages,
            load_messages=load_messages,
            store_inputs=store_inputs,
            store_context_messages=store_context_messages,
            store_context_from=store_context_from,
            store_outputs=store_outputs,
            **self._shared(retention),
        )

    def session_store(self, *, retention: RetentionPolicy | None = _HUB_POLICY) -> PostgresSessionStore:
        """A session store on the shared pool; ``retention`` defaults to the hub's."""
        return PostgresSessionStore(**self._shared(retention))

    def checkpoint_storage(
        self,
        *,
        tenant_id: str | None = None,
        scope: str | None = None,
        allowed_checkpoint_types: list[str] | None = None,
        retention: RetentionPolicy | None = _HUB_POLICY,
    ) -> PostgresCheckpointStorage:
        """A checkpoint storage on the shared pool, optionally scoped to one tenant and one conversation."""
        return PostgresCheckpointStorage(
            tenant_id=tenant_id,
            scope=scope,
            allowed_checkpoint_types=allowed_checkpoint_types,
            **self._shared(retention),
        )

    def thread_snapshot_store(
        self, *, retention: RetentionPolicy | None = _HUB_POLICY
    ) -> PostgresAGUIThreadSnapshotStore:
        """An AG-UI thread snapshot store on the shared pool (needs the ``ag-ui`` extra)."""
        from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore

        return PostgresAGUIThreadSnapshotStore(**self._shared(retention))

    def leases(self) -> PostgresLeases:
        """The lease table on the shared pool."""
        return PostgresLeases(
            application_id=self.application_id,
            client=self._client,
            schema=self.names.schema,
            table_prefix=self.names.prefix,
        )

    async def purge(self, mode: RetentionMode | None = None) -> PurgeReport:
        """Purge every expired row of this application in every table.

        A row is expired when its ``expires_at``, stamped by whichever policy wrote it, has
        passed; the hub needs no policy of its own. ``mode`` defaults to the hub policy's mode.
        Each table is purged in batches, one transaction per batch. A history session is purged
        whole, and only once every one of its messages has expired.
        """
        chosen = RetentionPolicy(mode=mode).mode if mode is not None else self.retention.mode
        where = sql.SQL("application_id = %(app)s")
        params = {"app": self.application_id}
        counts: dict[str, int] = {
            f"{self.names.prefix}history_messages": await purge_history(
                self._client, self.names, application_id=self.application_id
            )
        }
        plan: list[tuple[str, sql.Composable, sql.Composable | None, bool]] = [
            ("sessions", where, sql.SQL("snapshot = NULL"), True),
            ("checkpoints", where, None, False),
            (
                "thread_snapshots",
                where,
                sql.SQL("messages = NULL, state = NULL, interrupt = NULL, session_state = NULL"),
                True,
            ),
            ("leases", where, None, False),
        ]
        # One transaction per batch: row locks are never held across tables, so a long purge cannot
        # deadlock with, say, a history trim on the same session.
        for table, table_where, tombstone, tombstone_allowed in plan:
            counts[f"{self.names.prefix}{table}"] = await purge_rows(
                self._client,
                table=self.names.table(table),
                where=table_where,
                params=params,
                mode=chosen if tombstone_allowed else "delete",
                tombstone=tombstone,
            )
        return PurgeReport(counts)
