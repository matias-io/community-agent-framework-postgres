"""One pool, one schema, every store: the entry point most applications want."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from agent_framework import SecretString
from psycopg import sql

from ._checkpoint_storage import PostgresCheckpointStorage
from ._client import PostgresClient, TableNames, create_client, require_text
from ._document_store import PostgresDocumentStore
from ._history_provider import PostgresHistoryProvider
from ._leases import PostgresLeases
from ._migrations import MigrationReport, migrate, pending_versions
from ._retention import PurgeReport, RetentionPolicy, purge_rows
from ._session_store import PostgresSessionStore

if TYPE_CHECKING:
    from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore


class PostgresPersistence:
    """Own one connection pool and hand it to every store this package provides.

    Enter it (``async with``) or call ``open()`` to open the pool up front; otherwise the
    pool opens on first use. Stores it creates borrow the pool and never close it.
    """

    def __init__(
        self,
        *,
        application_id: str,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
    ) -> None:
        self.application_id = require_text(application_id, "application_id")
        self.names = TableNames(schema=schema, prefix=table_prefix)
        self.retention = retention or RetentionPolicy()
        self._client = create_client(
            connection_string, client=client, env_file_path=env_file_path, env_file_encoding=env_file_encoding
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

    async def migrate(self) -> MigrationReport:
        """Create or upgrade the schema."""
        return await migrate(self._client, self.names)

    async def pending_migrations(self) -> list[int]:
        """Versions ``migrate`` would apply."""
        return await pending_versions(self._client, self.names)

    def _common(self, overrides: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {
            "application_id": self.application_id,
            "client": self.pool,
            "schema": self.names.schema,
            "table_prefix": self.names.prefix,
            "retention": self.retention,
        }
        return base | overrides

    def history_provider(
        self, source_id: str = PostgresHistoryProvider.DEFAULT_SOURCE_ID, **kwargs: Any
    ) -> PostgresHistoryProvider:
        """A history provider on the shared pool; keyword arguments as on the class."""
        return PostgresHistoryProvider(source_id, **self._common(kwargs))

    def session_store(self, **kwargs: Any) -> PostgresSessionStore:
        """A session store on the shared pool."""
        return PostgresSessionStore(**self._common(kwargs))

    def checkpoint_storage(
        self, *, scope: str | None = None, allowed_checkpoint_types: list[str] | None = None, **kwargs: Any
    ) -> PostgresCheckpointStorage:
        """A checkpoint storage on the shared pool, optionally scoped to one conversation."""
        return PostgresCheckpointStorage(
            scope=scope, allowed_checkpoint_types=allowed_checkpoint_types, **self._common(kwargs)
        )

    def thread_snapshot_store(self, **kwargs: Any) -> PostgresAGUIThreadSnapshotStore:
        """An AG-UI thread snapshot store on the shared pool (needs the ``ag-ui`` extra)."""
        from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore

        return PostgresAGUIThreadSnapshotStore(**self._common(kwargs))

    def document_store(self, *, collection: str, **kwargs: Any) -> PostgresDocumentStore:
        """A document store for one collection on the shared pool."""
        return PostgresDocumentStore(collection=collection, **self._common(kwargs))

    def leases(self) -> PostgresLeases:
        """The lease table on the shared pool."""
        return PostgresLeases(
            application_id=self.application_id,
            client=self.pool,
            schema=self.names.schema,
            table_prefix=self.names.prefix,
        )

    async def purge(self) -> PurgeReport:
        """Apply the hub's retention policy to every table of this application."""
        if not self.retention.enabled:
            return PurgeReport()
        where = sql.SQL("application_id = %(app)s")
        params = {"app": self.application_id}
        plan: list[tuple[str, sql.Composable | None, bool]] = [
            ("history_messages", None, False),
            ("sessions", sql.SQL("snapshot = NULL"), True),
            ("checkpoints", None, False),
            (
                "thread_snapshots",
                sql.SQL("messages = NULL, state = NULL, interrupt = NULL, session_state = NULL"),
                True,
            ),
            ("documents", sql.SQL("payload = NULL"), True),
        ]
        counts: dict[str, int] = {}
        async with self._client.connection() as connection:
            for table, tombstone, tombstone_allowed in plan:
                counts[f"{self.names.prefix}{table}"] = await purge_rows(
                    connection,
                    table=self.names.table(table),
                    where=where,
                    params=params,
                    mode=self.retention.mode if tombstone_allowed else "delete",
                    tombstone=tombstone,
                )
        return PurgeReport(counts)
