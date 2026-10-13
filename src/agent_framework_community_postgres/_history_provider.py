"""Conversation history in PostgreSQL, one row per message, scoped like the Redis provider."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, cast

from agent_framework import HistoryProvider, Message, SecretString
from psycopg import AsyncConnection, sql

from ._client import ClientHandle, PostgresClient, PostgresStorageError, TableNames, optional_text, require_text
from ._entra import EntraCredential
from ._framework import filter_new_messages
from ._json import encode_jsonb
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy
from ._store import BaseStore

logger = logging.getLogger(__name__)


def _session_lock_key(
    tenant: sql.Composable, agent: sql.Composable, source: sql.Composable, session: sql.Composable
) -> sql.Composed:
    """The advisory lock key of one session's history; saves, ``clear`` and the purge all take it."""
    return sql.SQL(
        "hashtextextended(concat_ws(chr(31), %(schema)s::text, %(prefix)s::text, %(app)s::text, {}, {}, {}, {}), 0)"
    ).format(tenant, agent, source, session)


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

    _LOCK = sql.SQL("SELECT pg_advisory_xact_lock({})").format(
        _session_lock_key(
            sql.SQL("%(tenant)s::text"),
            sql.SQL("%(agent)s::text"),
            sql.SQL("%(source)s::text"),
            sql.SQL("%(session)s::text"),
        )
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
                # The subquery finds the newest message past the limit; it is NULL, and nothing is
                # deleted, while the session is within the limit.
                await connection.execute(
                    sql.SQL(
                        "DELETE FROM {history} WHERE {scope} AND id <="
                        " (SELECT id FROM {history} WHERE {scope} ORDER BY id DESC OFFSET %(keep)s LIMIT 1)"
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
        # One index probe per distinct session (a skip scan), instead of reading every message of every
        # session on the page; the recursion stops once the outer LIMIT has its rows. The rows come out in
        # recursion order, which is sorted; an outer ORDER BY would read the whole scope before the LIMIT.
        async with self._client.connection(transactional=False) as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "WITH RECURSIVE ids(session_id) AS ("
                    " (SELECT session_id FROM {history} WHERE {scope} AND session_id > %(after)s"
                    " ORDER BY session_id LIMIT 1)"
                    " UNION ALL"
                    " SELECT (SELECT h.session_id FROM {history} h WHERE {scope} AND h.session_id > ids.session_id"
                    " ORDER BY h.session_id LIMIT 1) FROM ids WHERE ids.session_id IS NOT NULL)"
                    " SELECT session_id FROM ids WHERE session_id IS NOT NULL LIMIT %(limit)s"
                ).format(history=self._names.table("history_messages"), scope=self._SESSIONS_SCOPE),
                # Session ids are never empty, so '' sorts before every one of them.
                self._scope_params() | {"after": "" if after is None else after, "limit": limit},
            )
            return [row[0] for row in await cursor.fetchall()]

    _SESSIONS_SCOPE = sql.SQL(
        "application_id = %(app)s AND tenant_id = %(tenant)s AND agent_id = %(agent)s AND source_id = %(source)s"
    )

    async def purge(self) -> PurgeReport:
        """Delete the messages of sessions whose every message has expired, whatever policy stamped them."""
        count = await purge_history(
            self._client,
            self._names,
            application_id=self.application_id,
            scope={"tenant": self.tenant_id, "agent": self.agent_id, "source": self.source_id},
        )
        return PurgeReport({f"{self._names.prefix}history_messages": count})


# Session keys passed as four parallel text arrays, one set for the sessions to delete and one for those to skip.
_KEYS = sql.SQL(
    "unnest(%(tenants)s::text[], %(agents)s::text[], %(sources)s::text[], %(sessions)s::text[]) AS k(t, a, s, sess)"
)
_SKIPPED_KEYS = sql.SQL(
    "unnest(%(skip_tenants)s::text[], %(skip_agents)s::text[], %(skip_sources)s::text[], %(skip_sessions)s::text[])"
    " AS k(t, a, s, sess)"
)


def _no_live_message(history: sql.Identifier, row: str) -> sql.Composed:
    """No message of ``row``'s session is still live: none has an expiry still ahead, or no expiry at all."""
    return sql.SQL(
        "NOT EXISTS (SELECT 1 FROM {history} live"
        " WHERE live.application_id = {row}.application_id AND live.tenant_id = {row}.tenant_id"
        " AND live.agent_id = {row}.agent_id AND live.source_id = {row}.source_id"
        " AND live.session_id = {row}.session_id AND (live.expires_at IS NULL OR live.expires_at >= now()))"
    ).format(history=history, row=sql.Identifier(row))


def _key_arrays(prefix: str, keys: Sequence[tuple[str, str, str, str]]) -> dict[str, list[str]]:
    return {
        f"{prefix}tenants": [key[0] for key in keys],
        f"{prefix}agents": [key[1] for key in keys],
        f"{prefix}sources": [key[2] for key in keys],
        f"{prefix}sessions": [key[3] for key in keys],
    }


async def purge_history(
    client: ClientHandle,
    names: TableNames,
    *,
    application_id: str,
    scope: Mapping[str, str] | None = None,
    batch_size: int = 1000,
) -> int:
    """Delete every message of each session whose messages have all expired; return the count.

    A session is deleted whole or not at all: a row written while ``ttl`` was ``None`` has no expiry
    and keeps its session, so a conversation that goes on never loses its head and a tool call never
    loses its result. Each transaction finds the sessions of up to ``batch_size`` expired messages
    whose sessions have no live message, tries those sessions' save locks, and deletes the sessions it
    locked, checking again in a statement that starts after the lock, so a save that committed
    meanwhile keeps its session. A session another writer holds is skipped for the rest of this
    purge; a later purge takes it. ``scope`` (``tenant``, ``agent``, ``source``) limits the purge to
    one provider's rows; without it the whole application is purged.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    history = names.table("history_messages")
    params: dict[str, Any] = {
        "app": application_id,
        "schema": names.schema,
        "prefix": names.prefix,
        "batch": batch_size,
    }
    narrow = sql.SQL("")
    if scope is not None:
        narrow = sql.SQL(" AND c.tenant_id = %(tenant)s AND c.agent_id = %(agent)s AND c.source_id = %(source)s")
        params |= dict(scope)
    # Finding the sessions and trying their locks is one statement; the delete runs after it, so its own
    # liveness check sees every save that committed before the lock was taken.
    find_and_lock = sql.SQL(
        "SELECT d.tenant_id, d.agent_id, d.source_id, d.session_id, pg_try_advisory_xact_lock({key})"
        " FROM (SELECT DISTINCT c.tenant_id, c.agent_id, c.source_id, c.session_id FROM"
        " (SELECT c.tenant_id, c.agent_id, c.source_id, c.session_id FROM {history} c"
        " WHERE c.application_id = %(app)s{narrow} AND c.expires_at < now() AND {no_live}"
        " AND (c.tenant_id, c.agent_id, c.source_id, c.session_id) NOT IN (SELECT k.t, k.a, k.s, k.sess FROM {skipped})"
        " LIMIT %(batch)s) c) d"
    ).format(
        key=_session_lock_key(
            sql.SQL("d.tenant_id"), sql.SQL("d.agent_id"), sql.SQL("d.source_id"), sql.SQL("d.session_id")
        ),
        history=history,
        narrow=narrow,
        no_live=_no_live_message(history, "c"),
        skipped=_SKIPPED_KEYS,
    )
    delete = sql.SQL(
        "DELETE FROM {history} h USING {keys} WHERE h.application_id = %(app)s AND h.tenant_id = k.t"
        " AND h.agent_id = k.a AND h.source_id = k.s AND h.session_id = k.sess AND {no_live}"
    ).format(history=history, keys=_KEYS, no_live=_no_live_message(history, "h"))

    skipped: list[tuple[str, str, str, str]] = []
    total = 0
    while True:
        async with client.connection(read_committed=True) as connection:
            cursor = await connection.execute(find_and_lock, params | _key_arrays("skip_", skipped))
            found = await cursor.fetchall()
            locked = [(row[0], row[1], row[2], row[3]) for row in found if row[4]]
            # Every round deletes or skips each session it found, so the loop ends once none is left.
            skipped.extend((row[0], row[1], row[2], row[3]) for row in found if not row[4])
            if locked:
                cursor = await connection.execute(delete, params | _key_arrays("", locked))
                total += cursor.rowcount
        if not found:
            return total
