"""Conversation history in PostgreSQL, one row per message, scoped like the Redis provider."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from agent_framework import HistoryProvider, Message, SecretString
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from ._client import PostgresClient, optional_text, require_text
from ._framework import filter_new_messages
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore

logger = logging.getLogger(__name__)


class PostgresHistoryProvider(HistoryProvider, BaseStore):
    """PostgreSQL-backed ``HistoryProvider``.

    Rows are isolated by application, optional tenant and agent, provider ``source_id``
    and session id, the same boundaries ``RedisHistoryProvider`` uses. These identifiers
    select stored history; they are not authorization. Bind them to authenticated
    context in the application.

    Saves to one session are serialized by a transaction-scoped advisory lock, so
    concurrent writers never duplicate history. When an agent runs without an explicit
    session, MAF creates a new session id per call, so each stateless run writes a new
    history; pass a session or set ``retention``.
    """

    DEFAULT_SOURCE_ID: ClassVar[str] = "postgres_history"

    def __init__(
        self,
        source_id: str = DEFAULT_SOURCE_ID,
        *,
        application_id: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        max_messages: int | None = None,
        load_messages: bool = True,
        store_inputs: bool = True,
        store_context_messages: bool = False,
        store_context_from: set[str] | None = None,
        store_outputs: bool = True,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
    ) -> None:
        HistoryProvider.__init__(
            self,
            require_text(source_id, "source_id"),
            load_messages=load_messages,
            store_inputs=store_inputs,
            store_context_messages=store_context_messages,
            store_context_from=store_context_from,
            store_outputs=store_outputs,
        )
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
        if max_messages is not None and max_messages < 0:
            raise ValueError("max_messages must be None (unlimited) or a non-negative integer.")
        self.tenant_id = optional_text(tenant_id, "tenant_id")
        self.agent_id = optional_text(agent_id, "agent_id")
        self.max_messages = max_messages

    _SCOPE = sql.SQL(
        "application_id = %(app)s AND tenant_id = %(tenant)s AND agent_id = %(agent)s"
        " AND source_id = %(source)s AND session_id = %(session)s"
    )

    def _params(self, session_id: str | None) -> dict[str, Any]:
        return {
            "app": self.application_id,
            "tenant": self.tenant_id,
            "agent": self.agent_id,
            "source": self.source_id,
            "session": require_text(session_id, "session_id"),
        }

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """All stored messages for the session, oldest first."""
        params = self._params(session_id)
        async with self._client.connection() as connection:
            return await self._read(connection, params)

    async def _read(self, connection: AsyncConnection[Any], params: dict[str, Any]) -> list[Message]:
        cursor = await connection.execute(
            sql.SQL("SELECT message FROM {history} WHERE {scope} ORDER BY id").format(
                history=self._names.table("history_messages"), scope=self._SCOPE
            ),
            params,
        )
        messages: list[Message] = []
        for (payload,) in await cursor.fetchall():
            if not isinstance(payload, dict):
                logger.warning("Skipping a history row whose message is not an object.")
                continue
            try:
                messages.append(Message.from_dict(cast("dict[str, Any]", payload)))
            except (ValueError, TypeError, KeyError, AttributeError):
                logger.warning("Skipping a history row that failed to deserialize.")
        return messages

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append the messages not already stored; trim to ``max_messages`` when set."""
        params = self._params(session_id)
        if not messages or self.max_messages == 0:
            return
        history = self._names.table("history_messages")
        async with self._client.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "concat_ws(chr(31), %(app)s::text, %(tenant)s::text, %(agent)s::text, %(source)s::text,"
                " %(session)s::text), 0))",
                params,
            )
            existing = await self._read(connection, params)
            new_messages = filter_new_messages(existing, messages)
            if not new_messages:
                return
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {history}"
                        " (application_id, tenant_id, agent_id, source_id, session_id, message, expires_at)"
                        " VALUES (%(app)s, %(tenant)s, %(agent)s, %(source)s, %(session)s, %(message)s, {expires})"
                    ).format(history=history, expires=EXPIRES_AT),
                    [params | {"message": Jsonb(message.to_dict()), "ttl": self._ttl()} for message in new_messages],
                )
            if self.max_messages is not None:
                await connection.execute(
                    sql.SQL(
                        "DELETE FROM {history} WHERE {scope} AND id NOT IN"
                        " (SELECT id FROM {history} WHERE {scope} ORDER BY id DESC LIMIT %(keep)s)"
                    ).format(history=history, scope=self._SCOPE),
                    params | {"keep": self.max_messages},
                )

    async def clear(self, session_id: str | None) -> None:
        """Delete every stored message for the session."""
        params = self._params(session_id)
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL("DELETE FROM {history} WHERE {scope}").format(
                    history=self._names.table("history_messages"), scope=self._SCOPE
                ),
                params,
            )

    async def list_sessions(self) -> list[str]:
        """Session ids with stored history under this provider's scope, sorted."""
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT DISTINCT session_id FROM {history}"
                    " WHERE application_id = %(app)s AND tenant_id = %(tenant)s AND agent_id = %(agent)s"
                    " AND source_id = %(source)s ORDER BY session_id"
                ).format(history=self._names.table("history_messages")),
                {
                    "app": self.application_id,
                    "tenant": self.tenant_id,
                    "agent": self.agent_id,
                    "source": self.source_id,
                },
            )
            return [row[0] for row in await cursor.fetchall()]

    async def purge(self) -> PurgeReport:
        """Delete expired messages under this provider's scope (history has no tombstone form)."""
        if not self._retention.enabled:
            return PurgeReport()
        async with self._client.connection() as connection:
            count = await purge_rows(
                connection,
                table=self._names.table("history_messages"),
                where=sql.SQL(
                    "application_id = %(app)s AND tenant_id = %(tenant)s AND agent_id = %(agent)s"
                    " AND source_id = %(source)s"
                ),
                params={
                    "app": self.application_id,
                    "tenant": self.tenant_id,
                    "agent": self.agent_id,
                    "source": self.source_id,
                },
                mode="delete",
                tombstone=None,
            )
        return PurgeReport({f"{self._names.prefix}history_messages": count})
