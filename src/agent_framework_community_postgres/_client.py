"""Connection ownership, settings, table naming and this package's error types."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias, TypedDict, cast

from agent_framework import SecretString, load_settings
from agent_framework.exceptions import IntegrationException
from psycopg import AsyncConnection, Error, OperationalError, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

if TYPE_CHECKING:
    # _entra imports this module's error type, so it is imported where a pool is built.
    from ._entra import EntraCredential

logger = logging.getLogger(__name__)

PostgresClient: TypeAlias = AsyncConnection[Any] | AsyncConnectionPool[AsyncConnection[Any]]

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
_PREFIX = re.compile(r"[a-z0-9_]*")
# The longest table or index suffix this package creates; a migration adding a longer one must update it.
_LONGEST_IDENTIFIER = "history_messages_session_expiry_idx"
_MAX_IDENTIFIER_BYTES = 63
# Bounds one connection attempt and how long a caller waits for a pooled connection. The pool
# keeps retrying in the background, so a call fails fast and a later call recovers with the database.
_CONNECT_TIMEOUT_SECONDS = 10
# Connection parameters for an owned pool when the connection string does not set them. Keepalives
# and tcp_user_timeout bound a silently dead connection to about a minute instead of the operating
# system's retransmit timeout. No statement or lock timeout: history saves and replicas waiting on
# the migration lock are meant to wait.
_CONNECTION_DEFAULTS: dict[str, Any] = {
    "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 30000,
}
_POOL_DEFAULTS: dict[str, Any] = {"min_size": 1, "max_size": 10, "timeout": float(_CONNECT_TIMEOUT_SECONDS)}
# SQLSTATE classes a caller may retry on a fresh connection: connection failure, transaction rollback
# (serialization, deadlock), insufficient resources, operator intervention, lock not available.
_RETRYABLE_SQLSTATES = ("08", "40", "53", "57P0", "55P03")
_WEAK_SSLMODES = ("disable", "allow", "prefer")
# Encrypted, but the server's certificate (require) or host name (verify-ca) goes unchecked.
_UNVERIFIED_SSLMODES = ("require", "verify-ca")
# One lock per borrowed connection, shared by every handle over it: a psycopg connection runs one
# transaction block at a time, so overlapping calls from two tasks would nest out of order.
_CONNECTION_LOCKS: weakref.WeakKeyDictionary[AsyncConnection[Any], asyncio.Lock] = weakref.WeakKeyDictionary()


class PostgresSettings(TypedDict, total=False):
    """Connection settings resolved from an explicit value, a selected ``.env`` file, or ``POSTGRES_*``."""

    connection_string: SecretString | None
    """Psycopg conninfo or URI; from ``POSTGRES_CONNECTION_STRING`` when not given explicitly."""


def is_retryable(sqlstate: str | None) -> bool:
    """Whether a driver error with this SQLSTATE is worth retrying on a fresh connection."""
    return sqlstate is not None and sqlstate.startswith(_RETRYABLE_SQLSTATES)


class PostgresStorageError(IntegrationException):
    """A PostgreSQL operation failed; the driver exception is chained as the cause.

    ``sqlstate`` is the server's error code when there was one. ``retryable`` is ``True`` for
    connection exceptions (class 08), transaction rollbacks such as serialization failures and
    deadlocks (class 40), insufficient resources (class 53), server shutdown and admin
    termination (57P0x), lock not available, which includes ``lock_timeout`` (55P03), pool
    timeouts, and a connection lost without a SQLSTATE.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.retryable = retryable


class _LeaseError(PostgresStorageError):
    """A lease error; the message names no resource, which ``resource`` carries."""

    def __init__(self, message: str, *, resource: str | None = None) -> None:
        super().__init__(message)
        self.resource = resource


class LeaseUnavailable(_LeaseError):
    """Someone holds the lease and the wait budget ran out."""


class LeaseLost(_LeaseError):
    """The lease expired or was taken over before ``renew`` ran."""


def require_text(value: object, name: str) -> str:
    """Return ``value`` when it is a non-empty ``str``; raise ``ValueError`` otherwise."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def optional_text(value: object, name: str) -> str:
    """Map ``None`` to ``''`` for storage; reject an explicit ``''`` so absence and value never conflate."""
    if value is None:
        return ""
    return require_text(value, name)


@dataclass(frozen=True)
class TableNames:
    """Schema and prefix every table of this package is created under."""

    schema: str = "public"
    prefix: str = "af_"

    def __post_init__(self) -> None:
        if not _SCHEMA.fullmatch(self.schema):
            raise ValueError("schema must be a lowercase PostgreSQL identifier (letters, digits, underscores).")
        if self.schema.startswith("pg_"):
            raise ValueError("schema must not start with 'pg_'; PostgreSQL reserves that prefix for system schemas.")
        if not _PREFIX.fullmatch(self.prefix):
            raise ValueError("table_prefix may contain only lowercase letters, digits and underscores.")
        for name in (self.schema, f"{self.prefix}{_LONGEST_IDENTIFIER}"):
            if len(name.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
                raise ValueError("schema and table_prefix must keep every identifier within 63 bytes.")

    def table(self, name: str) -> sql.Identifier:
        """The schema-qualified identifier of one of this package's tables."""
        return sql.Identifier(self.schema, f"{self.prefix}{name}")

    def index(self, name: str) -> sql.Identifier:
        """An unqualified index identifier (``CREATE INDEX`` takes the schema from the table)."""
        return sql.Identifier(f"{self.prefix}{name}")

    def qualified(self, name: str) -> str:
        """``schema.table`` as plain text, for catalog lookups."""
        return f"{self.schema}.{self.prefix}{name}"


def _refuse_weak_sslmode(options: Mapping[str, Any], kwargs: Mapping[str, Any]) -> None:
    """With a credential the password is a bearer token for every Azure server; refuse an unverified channel."""
    # libpq's precedence: a keyword argument, then the connection string, then PGSSLMODE.
    for source, value in (
        ("sslmode", kwargs.get("sslmode")),
        ("sslmode", options.get("sslmode")),
        ("PGSSLMODE", os.environ.get("PGSSLMODE")),
    ):
        if not value:
            continue
        if value in _WEAK_SSLMODES:
            raise ValueError(
                f"{source}={value} sends the Microsoft Entra ID token without verifying the server;"
                " use verify-full, the default with a credential."
            )
        if value in _UNVERIFIED_SSLMODES:
            logger.warning(
                "%s=%s encrypts the connection but does not verify the server's %s, so the Microsoft Entra ID"
                " token can reach an impostor; use verify-full, the default with a credential.",
                source,
                value,
                "certificate" if value == "require" else "host name",
            )
        return


@asynccontextmanager
async def _block(
    connection: AsyncConnection[Any], transactional: bool, read_committed: bool, *, started: bool
) -> AsyncGenerator[None]:
    """A transaction (or savepoint) around the caller's statements, or nothing for an autocommit read."""
    # A borrowed connection outside autocommit would leave the read's implicit transaction open, so
    # its next write would become a savepoint nobody commits; such a read still gets a transaction.
    if not transactional and (connection.autocommit or not started):
        yield
        return
    async with connection.transaction():
        if read_committed and started:
            # Must be the transaction's first statement; a snapshot taken earlier would be kept.
            await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        yield


class ClientHandle:
    """Own a lazily opened pool, or borrow a caller's pool or connection without closing it.

    With ``credential``, the owned pool signs in to Azure Database for PostgreSQL with a Microsoft
    Entra ID token per new connection (see ``_entra``).
    """

    def __init__(
        self,
        connection_string: SecretString | None,
        client: PostgresClient | None,
        *,
        credential: EntraCredential | None = None,
        pool_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if (connection_string is None) == (client is None):
            raise ValueError("Supply exactly one of connection_string or client.")
        if credential is not None and client is not None:
            raise ValueError(
                "credential cannot be combined with client; a pool or connection you pass signs in itself."
            )
        if pool_kwargs is not None and client is not None:
            raise ValueError("pool_kwargs cannot be combined with client; it configures a pool this handle owns.")
        if client is not None and not isinstance(client, (AsyncConnection, AsyncConnectionPool)):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("client must be a psycopg AsyncConnection or AsyncConnectionPool.")
        self.owned = client is None
        self.client: PostgresClient
        # Signs in once without connecting; set only for an owned pool with a credential.
        self._check_credential: Callable[[], Awaitable[None]] | None = None
        if connection_string is not None:
            conninfo = connection_string.get_secret_value()
            if not conninfo.strip():
                raise ValueError("connection_string must not be empty.")
            try:
                options = conninfo_to_dict(conninfo)
            except Error:
                # from None: libpq's text can quote a token next to the password.
                raise PostgresStorageError("Invalid connection string.") from None
            overrides = dict(pool_kwargs or {})
            connection_kwargs: dict[str, Any] = {"autocommit": True}
            connection_kwargs.update({key: value for key, value in _CONNECTION_DEFAULTS.items() if key not in options})
            connection_kwargs.update(overrides.pop("kwargs", {}))
            pool_options = _POOL_DEFAULTS | overrides
            if credential is not None:
                from ._entra import check_credential, entra_connection_class

                if "connection_class" in pool_options:
                    raise ValueError("pool_kwargs cannot set connection_class together with credential.")
                _refuse_weak_sslmode(options, connection_kwargs)
                pool_options["connection_class"] = entra_connection_class(credential)
                self._check_credential = functools.partial(
                    check_credential, credential, conninfo, dict(connection_kwargs)
                )
            self.client = AsyncConnectionPool(conninfo, open=False, kwargs=connection_kwargs, **pool_options)
        else:
            self.client = cast(PostgresClient, client)
        self.closed = False
        self._parent: ClientHandle | None = None

    @staticmethod
    def _no_connection(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> PostgresStorageError:
        # psycopg_pool raises the same PoolTimeout for a database that is down and for a pool whose
        # connections are all busy, so the message names both.
        seconds = f"{pool.timeout:g} second{'' if pool.timeout == 1 else 's'}"
        message = (
            f"No PostgreSQL connection became available within {seconds}: the database is unreachable or"
            " every pooled connection is busy; psycopg_pool logs the driver's reason."
        )
        # Set only on an Entra ID connection class (see _entra); the message names no token.
        last_error = getattr(pool.connection_class, "entra_last_error", None)
        if isinstance(last_error, str):
            message += f" Last connection error: {last_error}"
        return PostgresStorageError(message, retryable=True)

    def __repr__(self) -> str:
        kind = type(self.client).__name__
        return f"ClientHandle(owned={self.owned}, client={kind}, closed={self.closed})"

    async def open(self) -> None:
        """Open an owned pool; a no-op for a borrowed client.

        With a credential, one token is fetched before the pool opens, so a failing credential raises
        ``PostgresStorageError`` here instead of a pool timeout later. Nothing is cached.
        """
        self._ensure_open()
        if self.owned and isinstance(self.client, AsyncConnectionPool):
            if self._check_credential is not None and self.client.closed:
                await self._check_credential()
            await self.client.open()

    def _root(self) -> ClientHandle:
        handle = self
        while handle._parent is not None:
            handle = handle._parent
        return handle

    def child(self) -> ClientHandle:
        """A borrowed handle over the same client that stops working once this handle is closed."""
        handle = ClientHandle(None, self.client)
        handle._parent = self
        return handle

    def _is_closed(self) -> bool:
        return self.closed or (self._parent is not None and self._parent._is_closed())

    def _ensure_open(self) -> None:
        if self._is_closed():
            raise PostgresStorageError("The Postgres client is closed.")

    @asynccontextmanager
    async def connection(
        self, *, transactional: bool = True, read_committed: bool = False
    ) -> AsyncGenerator[AsyncConnection[Any]]:
        """Yield a connection, inside a transaction unless ``transactional`` is ``False``.

        A single-statement read passes ``transactional=False`` and runs in autocommit, saving two round
        trips. ``read_committed`` makes ``READ COMMITTED`` the isolation level of a transaction this
        handle starts, whatever the server default; inside a caller's own transaction the caller's
        level applies. A pool is opened on first use; a borrowed pool its owner already closed raises
        ``PostgresStorageError``. A borrowed connection is held under a lock shared by every handle
        over it, so calls on one connection run one at a time; inside the caller's own transaction a
        transactional block is a savepoint. psycopg errors become ``PostgresStorageError``.
        """
        self._ensure_open()
        try:
            if isinstance(self.client, AsyncConnectionPool):
                root = self._root()
                if root.owned:
                    # The handle that owns the pool opens it, so a hub's stores get its credential check too.
                    await root.open()
                elif self.client.closed:
                    await self.client.open()
                async with (
                    self.client.connection() as connection,
                    _block(connection, transactional, read_committed, started=True),
                ):
                    yield connection
            else:
                lock = _CONNECTION_LOCKS.setdefault(self.client, asyncio.Lock())
                async with lock:
                    started = self.client.info.transaction_status == TransactionStatus.IDLE
                    async with _block(self.client, transactional, read_committed, started=started):
                        yield self.client
        except PoolTimeout as exc:
            if isinstance(self.client, AsyncConnectionPool):
                raise self._no_connection(self.client) from exc
            raise
        except Error as exc:
            sqlstate = exc.sqlstate
            retryable = is_retryable(sqlstate) or (
                sqlstate is None and isinstance(exc, OperationalError) and not isinstance(exc, PoolClosed)
            )
            if isinstance(self.client, AsyncConnection) and self.client.closed:
                # A retry would get the same dead connection; only a pool hands out a fresh one.
                retryable = False
            raise PostgresStorageError(
                "PostgreSQL operation failed; see the chained driver exception.", sqlstate=sqlstate, retryable=retryable
            ) from exc

    async def close(self) -> None:
        """Close an owned pool once; borrowed clients are left to their owner."""
        if not self.closed:
            if self.owned and isinstance(self.client, AsyncConnectionPool):
                await self.client.close()
            self.closed = True


def create_client(
    connection_string: str | SecretString | None,
    *,
    client: PostgresClient | ClientHandle | None,
    env_file_path: str | None,
    env_file_encoding: str | None,
    credential: EntraCredential | None = None,
    pool_kwargs: Mapping[str, Any] | None = None,
) -> ClientHandle:
    """Resolve the connection the way every store does: explicit argument, ``.env`` file, environment.

    A ``ClientHandle`` (how ``PostgresPersistence`` shares its pool) is borrowed as a child handle, so
    closing the handle it came from stops this one too. ``credential`` signs the owned pool in with
    Microsoft Entra ID and needs a connection string, not a client. ``pool_kwargs`` is merged over the
    owned pool's defaults, its ``kwargs`` entry over the connection defaults.
    """
    if client is not None:
        if credential is not None:
            raise ValueError(
                "credential cannot be combined with client; a pool or connection you pass signs in itself."
            )
        if (
            connection_string is not None
            or env_file_path is not None
            or env_file_encoding is not None
            or pool_kwargs is not None
        ):
            raise ValueError(
                "client cannot be combined with connection_string, env_file_path, env_file_encoding or pool_kwargs."
            )
        if isinstance(client, ClientHandle):
            return client.child()
        return ClientHandle(None, client)
    settings = load_settings(
        PostgresSettings,
        env_prefix="POSTGRES_",
        required_fields=["connection_string"],
        connection_string=connection_string,
        env_file_path=env_file_path,
        env_file_encoding=env_file_encoding,
    )
    resolved = settings.get("connection_string")
    if not isinstance(resolved, SecretString):
        raise TypeError("connection_string must be a string or SecretString.")
    return ClientHandle(resolved, None, credential=credential, pool_kwargs=pool_kwargs)
