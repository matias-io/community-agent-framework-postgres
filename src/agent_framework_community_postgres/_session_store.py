"""``AgentSession`` snapshots in PostgreSQL, one row per opaque session-store id."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from agent_framework import AgentSession, SecretString, SessionStore
from psycopg import sql
from psycopg.rows import dict_row

from ._client import ClientHandle, PostgresClient
from ._json import encode_jsonb
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore


class PostgresSessionStore(SessionStore, BaseStore):
    """PostgreSQL-backed ``SessionStore``.

    ``set`` stores ``AgentSession.to_dict()`` (custom state goes through
    ``register_state_type`` codecs) as JSONB; ``get`` rebuilds a fresh session with
    ``AgentSession.from_dict``. Restore a session with the same agent and provider
    configuration that created it, as the framework documents. ``SessionStore`` is
    marked experimental upstream; this implementation follows it.
    """

    SNAPSHOT_VERSION: ClassVar[str] = "1"

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
        SessionStore.__init__(self)
        BaseStore.__init__(
            self,
            application_id=application_id,
            connection_string=connection_string,
            client=client,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            schema=schema,
            table_prefix=table_prefix,
            retention=retention,
        )

    async def get(self, session_id: str) -> AgentSession | None:
        """A fresh copy of the stored session, or ``None`` when absent or purged."""
        SessionStore.validate_session_id(session_id)
        async with self._client.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                sql.SQL(
                    "SELECT snapshot, purged_at FROM {sessions} WHERE application_id = %(app)s AND session_id = %(id)s"
                ).format(sessions=self._names.table("sessions")),
                {"app": self.application_id, "id": session_id},
            )
            row = await cursor.fetchone()
        if row is None or row["purged_at"] is not None or not isinstance(row["snapshot"], dict):
            return None
        return AgentSession.from_dict(cast(dict[str, Any], row["snapshot"]))

    async def set(self, session_id: str, session: AgentSession) -> None:
        """Store the session, replacing any existing snapshot."""
        SessionStore.validate_session_id(session_id)
        snapshot = encode_jsonb(session.to_dict(), what="Session state")
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL(
                    "INSERT INTO {sessions} (application_id, session_id, snapshot, snapshot_version, expires_at)"
                    " VALUES (%(app)s, %(id)s, %(snapshot)s, %(version)s, {expires})"
                    " ON CONFLICT (application_id, session_id) DO UPDATE SET"
                    " snapshot = EXCLUDED.snapshot, snapshot_version = EXCLUDED.snapshot_version,"
                    " revision = {sessions}.revision + 1, updated_at = now(),"
                    " expires_at = EXCLUDED.expires_at, purged_at = NULL"
                ).format(sessions=self._names.table("sessions"), expires=EXPIRES_AT),
                {
                    "app": self.application_id,
                    "id": session_id,
                    "snapshot": snapshot,
                    "version": self.SNAPSHOT_VERSION,
                    "ttl": self._ttl(),
                },
            )

    async def delete(self, session_id: str) -> None:
        """Remove the stored session, if any."""
        SessionStore.validate_session_id(session_id)
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL("DELETE FROM {sessions} WHERE application_id = %(app)s AND session_id = %(id)s").format(
                    sessions=self._names.table("sessions")
                ),
                {"app": self.application_id, "id": session_id},
            )

    async def purge(self) -> PurgeReport:
        """Purge expired sessions in this store's mode, whatever policy stamped them."""
        async with self._client.connection() as connection:
            count = await purge_rows(
                connection,
                table=self._names.table("sessions"),
                where=sql.SQL("application_id = %(app)s"),
                params={"app": self.application_id},
                mode=self._retention.mode,
                tombstone=sql.SQL("snapshot = NULL"),
            )
        return PurgeReport({f"{self._names.prefix}sessions": count})
