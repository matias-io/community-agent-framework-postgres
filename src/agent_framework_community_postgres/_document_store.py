"""A scoped JSON document store with revisions and leases, for application state MAF does not model."""

from __future__ import annotations

import builtins
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agent_framework import SecretString
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ._client import PostgresClient, RevisionConflict, require_text
from ._leases import Lease, PostgresLeases
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore


@dataclass(frozen=True)
class Document:
    """One stored document."""

    payload: dict[str, Any]
    metadata: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class DocumentSummary:
    """A document without its payload, as ``list`` returns it."""

    key: str
    metadata: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    purged_at: datetime | None


class PostgresDocumentStore(BaseStore):
    """Documents keyed by ``(collection, scope, key)`` with a revision that increments on every write.

    ``scope`` is the caller's authorization boundary (a user, a tenant, an anonymous
    visitor); a key alone never reads a row. ``expected_revision`` gives optimistic
    concurrency: ``None`` upserts, ``0`` inserts only, ``n`` updates only when the
    stored revision is ``n``. ``lease`` serializes writers across processes.
    """

    def __init__(
        self,
        *,
        application_id: str,
        collection: str,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
    ) -> None:
        super().__init__(
            application_id=application_id,
            connection_string=connection_string,
            client=client,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            schema=schema,
            table_prefix=table_prefix,
            retention=retention,
        )
        self.collection = require_text(collection, "collection")
        self._leases = PostgresLeases(
            application_id=self.application_id, client=self._client.client, schema=schema, table_prefix=table_prefix
        )

    def _where_key(self) -> sql.SQL:
        return sql.SQL(
            "application_id = %(app)s AND collection = %(collection)s AND scope = %(scope)s AND key = %(key)s"
        )

    def _params(self, scope: str, key: str) -> dict[str, Any]:
        return {
            "app": self.application_id,
            "collection": self.collection,
            "scope": require_text(scope, "scope"),
            "key": require_text(key, "key"),
        }

    async def get(self, *, scope: str, key: str) -> Document | None:
        """The document, or ``None`` when absent or purged."""
        async with self._client.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                sql.SQL(
                    "SELECT payload, metadata, revision, created_at, updated_at, expires_at, purged_at"
                    " FROM {documents} WHERE {where}"
                ).format(documents=self._names.table("documents"), where=self._where_key()),
                self._params(scope, key),
            )
            row = await cursor.fetchone()
        if row is None or row["purged_at"] is not None or row["payload"] is None:
            return None
        return Document(
            payload=row["payload"],
            metadata=row["metadata"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    async def put(
        self,
        *,
        scope: str,
        key: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Write the document and return its new revision. ``metadata=None`` keeps the stored metadata."""
        if expected_revision is not None and expected_revision < 0:
            raise ValueError("expected_revision must be None, 0 or a positive integer.")
        params = self._params(scope, key) | {
            "payload": Jsonb(payload),
            "metadata": Jsonb(metadata) if metadata is not None else None,
            "ttl": self._ttl(),
            "expected": expected_revision,
        }
        documents = self._names.table("documents")
        if expected_revision is None:
            statement = sql.SQL(
                "INSERT INTO {documents} (application_id, collection, scope, key, payload, metadata, expires_at)"
                " VALUES (%(app)s, %(collection)s, %(scope)s, %(key)s, %(payload)s,"
                " coalesce(%(metadata)s, '{{}}'::jsonb), {expires})"
                " ON CONFLICT (application_id, collection, scope, key) DO UPDATE SET"
                " payload = EXCLUDED.payload, metadata = coalesce(%(metadata)s, {documents}.metadata),"
                " revision = {documents}.revision + 1, updated_at = now(),"
                " expires_at = EXCLUDED.expires_at, purged_at = NULL"
                " RETURNING revision"
            ).format(documents=documents, expires=EXPIRES_AT)
        elif expected_revision == 0:
            statement = sql.SQL(
                "INSERT INTO {documents} (application_id, collection, scope, key, payload, metadata, expires_at)"
                " VALUES (%(app)s, %(collection)s, %(scope)s, %(key)s, %(payload)s,"
                " coalesce(%(metadata)s, '{{}}'::jsonb), {expires})"
                " ON CONFLICT (application_id, collection, scope, key) DO NOTHING RETURNING revision"
            ).format(documents=documents, expires=EXPIRES_AT)
        else:
            statement = sql.SQL(
                "UPDATE {documents} SET payload = %(payload)s, metadata = coalesce(%(metadata)s, metadata),"
                " revision = revision + 1, updated_at = now(), expires_at = {expires}, purged_at = NULL"
                " WHERE {where} AND revision = %(expected)s RETURNING revision"
            ).format(documents=documents, expires=EXPIRES_AT, where=self._where_key())
        async with self._client.connection() as connection:
            cursor = await connection.execute(statement, params)
            row = await cursor.fetchone()
        if row is None:
            raise RevisionConflict(
                f"Document {key!r} in {self.collection!r} did not have revision {expected_revision}."
            )
        return int(row[0])

    async def delete(self, *, scope: str, key: str) -> bool:
        """Remove the document; ``True`` when a row existed."""
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("DELETE FROM {documents} WHERE {where}").format(
                    documents=self._names.table("documents"), where=self._where_key()
                ),
                self._params(scope, key),
            )
            return cursor.rowcount > 0

    async def list(
        self, *, scope: str, limit: int = 100, before: datetime | None = None, include_purged: bool = False
    ) -> builtins.list[DocumentSummary]:
        """Summaries in a scope, newest ``updated_at`` first; pass the last ``updated_at`` as ``before`` to page."""
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        params: dict[str, Any] = {
            "app": self.application_id,
            "collection": self.collection,
            "scope": require_text(scope, "scope"),
            "before": before,
            "limit": limit,
            "include_purged": include_purged,
        }
        async with self._client.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                sql.SQL(
                    "SELECT key, metadata, revision, created_at, updated_at, expires_at, purged_at"
                    " FROM {documents}"
                    " WHERE application_id = %(app)s AND collection = %(collection)s AND scope = %(scope)s"
                    " AND (%(before)s::timestamptz IS NULL OR updated_at < %(before)s::timestamptz)"
                    " AND (%(include_purged)s OR purged_at IS NULL)"
                    " ORDER BY updated_at DESC, key DESC LIMIT %(limit)s"
                ).format(documents=self._names.table("documents")),
                params,
            )
            rows = await cursor.fetchall()
        return [DocumentSummary(**row) for row in rows]

    def lease(
        self, *, scope: str, key: str, owner: str, ttl: timedelta, wait: timedelta = timedelta(0)
    ) -> AbstractAsyncContextManager[Lease]:
        """A lease named after this document; see ``PostgresLeases.acquire``."""
        resource = f"{self.collection}/{require_text(scope, 'scope')}/{require_text(key, 'key')}"
        return self._leases.acquire(resource, owner=owner, ttl=ttl, wait=wait)

    async def purge(self) -> PurgeReport:
        """Apply the retention policy to this collection's expired rows."""
        if not self._retention.enabled:
            return PurgeReport()
        async with self._client.connection() as connection:
            count = await purge_rows(
                connection,
                table=self._names.table("documents"),
                where=sql.SQL("application_id = %(app)s AND collection = %(collection)s"),
                params={"app": self.application_id, "collection": self.collection},
                mode=self._retention.mode,
                tombstone=sql.SQL("payload = NULL"),
            )
        return PurgeReport({f"{self._names.prefix}documents": count})
