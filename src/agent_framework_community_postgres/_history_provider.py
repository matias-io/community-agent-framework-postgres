"""Conversation history in PostgreSQL, one row per message, scoped like the Redis provider."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, cast

from agent_framework import HistoryProvider, Message, SecretString
from psycopg import AsyncConnection, sql

from ._client import ClientHandle, PostgresClient, PostgresStorageError, optional_text, require_text
from ._entra import EntraCredential
from ._framework import filter_new_messages
from ._json import encode_jsonb
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
        client: PostgresClient | ClientHandle | None = None,
        credential: EntraCredential | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
        pool_kwargs: Mapping[str, Any] | None = None,
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
            credential=credential,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            schema=schema,
            table_prefix=table_prefix,
            retention=retention,
            pool_kwargs=pool_kwargs,
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
        """All stored messages for the session, oldest first; set ``max_messages`` to bound the session."""
        params = self._params(session_id)
        async with self._client.connection(transactional=False) as connection:
            return await self._read(connection, params)

    @staticmethod
    def _decode(payload: Any) -> Message:
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        return Message.from_dict(cast("dict[str, Any]", payload))

    async def _read(self, connection: AsyncConnection[Any], params: dict[str, Any]) -> list[Message]:
        cursor = await connection.execute(
            sql.SQL("SELECT message FROM {history} WHERE {scope} ORDER BY id").format(
                history=self._names.table("history_messages"), scope=self._SCOPE
            ),
            params,
        )
        messages: list[Message] = []
        for (payload,) in await cursor.fetchall():
            try:
                messages.append(self._decode(payload))
            except (ValueError, TypeError, KeyError, AttributeError):
                logger.warning("Skipping a history row that failed to deserialize.")
        return messages

    async def _tail(self, connection: AsyncConnection[Any], params: dict[str, Any], limit: int) -> list[Message]:
        """The newest ``limit`` stored messages, oldest first; raises when one no longer deserializes."""
        cursor = await connection.execute(
            sql.SQL("SELECT message FROM {history} WHERE {scope} ORDER BY id DESC LIMIT %(limit)s").format(
                history=self._names.table("history_messages"), scope=self._SCOPE
            ),
            params | {"limit": limit},
        )
        rows = await cursor.fetchall()
        try:
            return [self._decode(payload) for (payload,) in reversed(rows)]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise PostgresStorageError(
                "A stored message in this session no longer deserializes; clear the session or repair the row."
            ) from exc

    _LOCK = sql.SQL(
        "SELECT pg_advisory_xact_lock(hashtextextended(concat_ws(chr(31), %(schema)s::text, %(prefix)s::text,"
        " %(app)s::text, %(tenant)s::text, %(agent)s::text, %(source)s::text, %(session)s::text), 0))"
    )

    def _lock_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return params | {"schema": self._names.schema, "prefix": self._names.prefix}

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append the messages not already stored; trim to ``max_messages`` when set.

        Writers of one session take turns on a transaction-scoped advisory lock inside a
        ``READ COMMITTED`` transaction, so the dedupe read sees every save committed before the
        lock was granted, whatever the server's default isolation level. Dedupe needs only the
        newest ``len(messages)`` stored rows: MAF's ``filter_new_messages`` compares a suffix of the
        stored history with a prefix of the batch, or the whole stored history inside the batch,
        and both fit in that window.
        """
        params = self._params(session_id)
        if not messages or self.max_messages == 0:
            return
        history = self._names.table("history_messages")
        # Encode before taking a connection, so a value JSONB refuses fails without holding one.
        encoded = {id(message): encode_jsonb(message.to_dict(), what="History message") for message in messages}
        async with self._client.connection(read_committed=True) as connection:
            await connection.execute(self._LOCK, self._lock_params(params))
            existing = await self._tail(connection, params, len(messages))
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
                    [params | {"message": encoded[id(message)], "ttl": self._ttl()} for message in new_messages],
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
        """Delete every stored message for the session; takes the session's save lock."""
        params = self._params(session_id)
        async with self._client.connection(read_committed=True) as connection:
            await connection.execute(self._LOCK, self._lock_params(params))
            await connection.execute(
                sql.SQL("DELETE FROM {history} WHERE {scope}").format(
                    history=self._names.table("history_messages"), scope=self._SCOPE
                ),
                params,
            )

    def _scope_params(self) -> dict[str, Any]:
        return {"app": self.application_id, "tenant": self.tenant_id, "agent": self.agent_id, "source": self.source_id}

    async def list_sessions(self, *, limit: int | None = None, after: str | None = None) -> list[str]:
        """Session ids with stored history under this provider's scope, sorted; page with ``limit`` and ``after``."""
        async with self._client.connection(transactional=False) as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT DISTINCT session_id FROM {history}"
                    " WHERE application_id = %(app)s AND tenant_id = %(tenant)s AND agent_id = %(agent)s"
                    " AND source_id = %(source)s AND (%(after)s::text IS NULL OR session_id > %(after)s)"
                    " ORDER BY session_id LIMIT %(limit)s"
                ).format(history=self._names.table("history_messages")),
                self._scope_params() | {"after": after, "limit": limit},
            )
            return [row[0] for row in await cursor.fetchall()]

    async def purge(self) -> PurgeReport:
        """Delete the messages of sessions whose every message has expired, whatever policy stamped them."""
        history = self._names.table("history_messages")
        count = await purge_rows(
            self._client,
            table=history,
            where=sql.SQL(
                "c.tenant_id = %(tenant)s AND c.agent_id = %(agent)s AND c.source_id = %(source)s AND {whole}"
            ).format(whole=whole_session_expired(history)),
            params=self._scope_params(),
            mode="delete",
            tombstone=None,
        )
        return PurgeReport({f"{self._names.prefix}history_messages": count})


def whole_session_expired(history: sql.Identifier) -> sql.Composable:
    """Purge predicate on the candidate row ``c``: no message of its session is still live.

    A session's rows are deleted only when no row of the session is still live; a row written while
    ``ttl`` was ``None`` has no expiry and keeps its whole session. A conversation that goes on
    never loses its head, and a tool call never loses its result. Nothing is rewritten on save; the
    rule is applied at purge time.
    """
    return sql.SQL(
        "c.application_id = %(app)s AND NOT EXISTS (SELECT 1 FROM {history} live"
        " WHERE live.application_id = c.application_id AND live.tenant_id = c.tenant_id"
        " AND live.agent_id = c.agent_id AND live.source_id = c.source_id AND live.session_id = c.session_id"
        " AND (live.expires_at IS NULL OR live.expires_at >= now()))"
    ).format(history=history)
