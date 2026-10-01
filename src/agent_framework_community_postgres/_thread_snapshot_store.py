"""Latest AG-UI Thread Snapshots in PostgreSQL, keyed by scope and thread id."""

from __future__ import annotations

from typing import Any

from agent_framework import SecretString
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ._client import ClientHandle, PostgresClient, require_text
from ._json import encode_jsonb
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore

try:
    from agent_framework_ag_ui import AGUIThreadSnapshot
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "PostgresAGUIThreadSnapshotStore needs agent-framework-ag-ui. "
        "Install community-agent-framework-postgres[ag-ui]."
    ) from exc


class PostgresAGUIThreadSnapshotStore(BaseStore):
    """PostgreSQL-backed ``AGUIThreadSnapshotStore``.

    One row per ``(scope, thread_id)`` holding the latest snapshot; ``save`` replaces it
    (last writer wins, as the in-memory store documents). ``scope`` must be the
    application's authorization boundary; a thread id alone never reads a row.
    """

    def __init__(
        self,
        *,
        application_id: str,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | ClientHandle | None = None,
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

    def _params(self, scope: str, thread_id: str) -> dict[str, Any]:
        return {
            "app": self.application_id,
            "scope": require_text(scope, "scope"),
            "thread": require_text(thread_id, "thread_id"),
        }

    _KEY = sql.SQL("application_id = %(app)s AND scope = %(scope)s AND thread_id = %(thread)s")

    @staticmethod
    def _jsonb(value: Any, what: str) -> Jsonb | None:
        return encode_jsonb(value, what=what) if value is not None else None

    async def save(self, *, scope: str, thread_id: str, snapshot: AGUIThreadSnapshot) -> None:
        """Store the latest snapshot for the thread, replacing the previous one."""
        values = {
            "messages": encode_jsonb(list(snapshot.messages), what="Thread snapshot messages"),
            "state": self._jsonb(snapshot.state, "Thread snapshot state"),
            "interrupt": self._jsonb(snapshot.interrupt, "Thread snapshot interrupt"),
            "session_state": self._jsonb(snapshot.session_state, "Thread snapshot session state"),
        }
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL(
                    "INSERT INTO {snapshots} (application_id, scope, thread_id, messages, state, interrupt,"
                    " session_state, expires_at)"
                    " VALUES (%(app)s, %(scope)s, %(thread)s, %(messages)s, %(state)s, %(interrupt)s,"
                    " %(session_state)s, {expires})"
                    " ON CONFLICT (application_id, scope, thread_id) DO UPDATE SET"
                    " messages = EXCLUDED.messages, state = EXCLUDED.state, interrupt = EXCLUDED.interrupt,"
                    " session_state = EXCLUDED.session_state, revision = {snapshots}.revision + 1,"
                    " updated_at = now(), expires_at = EXCLUDED.expires_at, purged_at = NULL"
                ).format(snapshots=self._names.table("thread_snapshots"), expires=EXPIRES_AT),
                self._params(scope, thread_id) | values | {"ttl": self._ttl()},
            )

    async def get(self, *, scope: str, thread_id: str) -> AGUIThreadSnapshot | None:
        """The latest snapshot, or ``None`` when absent or purged."""
        async with self._client.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                sql.SQL(
                    "SELECT messages, state, interrupt, session_state, purged_at FROM {snapshots} WHERE {key}"
                ).format(snapshots=self._names.table("thread_snapshots"), key=self._KEY),
                self._params(scope, thread_id),
            )
            row = await cursor.fetchone()
        if row is None or row["purged_at"] is not None or row["messages"] is None:
            return None
        return AGUIThreadSnapshot(
            messages=row["messages"], state=row["state"], interrupt=row["interrupt"], session_state=row["session_state"]
        )

    async def delete(self, *, scope: str, thread_id: str) -> bool:
        """Remove the thread's snapshot; ``True`` when a live one existed.

        A purged row is removed too, and returns ``False``.
        """
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("DELETE FROM {snapshots} WHERE {key} RETURNING purged_at IS NULL").format(
                    snapshots=self._names.table("thread_snapshots"), key=self._KEY
                ),
                self._params(scope, thread_id),
            )
            row = await cursor.fetchone()
        return row is not None and bool(row[0])

    async def clear(self, *, scope: str | None = None) -> None:
        """Remove every snapshot of this application, or only one scope's."""
        params: dict[str, Any] = {
            "app": self.application_id,
            "scope": require_text(scope, "scope") if scope is not None else None,
        }
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL(
                    "DELETE FROM {snapshots} WHERE application_id = %(app)s"
                    " AND (%(scope)s::text IS NULL OR scope = %(scope)s)"
                ).format(snapshots=self._names.table("thread_snapshots")),
                params,
            )

    async def purge(self) -> PurgeReport:
        """Purge expired snapshots in this store's mode, whatever policy stamped them."""
        async with self._client.connection() as connection:
            count = await purge_rows(
                connection,
                table=self._names.table("thread_snapshots"),
                where=sql.SQL("application_id = %(app)s"),
                params={"app": self.application_id},
                mode=self._retention.mode,
                tombstone=sql.SQL("messages = NULL, state = NULL, interrupt = NULL, session_state = NULL"),
            )
        return PurgeReport({f"{self._names.prefix}thread_snapshots": count})
