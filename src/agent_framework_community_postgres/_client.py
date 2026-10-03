"""Connection ownership, settings, table naming and this package's error types."""

from __future__ import annotations

import asyncio
import functools
import re
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias, TypedDict

from agent_framework import SecretString, load_settings
from agent_framework.exceptions import IntegrationException
from psycopg import AsyncConnection, Error, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool, PoolTimeout

if TYPE_CHECKING:
    # _entra imports this module's error type, so it is imported where a pool is built.
    from ._entra import EntraCredential

PostgresClient: TypeAlias = AsyncConnection[Any] | AsyncConnectionPool[AsyncConnection[Any]]

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
_PREFIX = re.compile(r"[a-z0-9_]*")
# The longest table or index suffix this package creates; a migration adding a longer one must update it.
_LONGEST_IDENTIFIER = "history_messages_session_idx"
_MAX_IDENTIFIER_BYTES = 63
# Bounds one connection attempt and how long a caller waits for a pooled connection. The pool
# keeps retrying in the background, so a call fails fast and a later call recovers with the database.
_CONNECT_TIMEOUT_SECONDS = 10
# One lock per borrowed connection, shared by every handle over it: a psycopg connection runs one
# transaction block at a time, so overlapping calls from two tasks would nest out of order.
_CONNECTION_LOCKS: weakref.WeakKeyDictionary[AsyncConnection[Any], asyncio.Lock] = weakref.WeakKeyDictionary()


class PostgresSettings(TypedDict, total=False):
    """Connection settings resolved from an explicit value, a selected ``.env`` file, or ``POSTGRES_*``."""

    connection_string: SecretString | None
    """Psycopg conninfo or URI; from ``POSTGRES_CONNECTION_STRING`` when not given explicitly."""


class PostgresStorageError(IntegrationException):
    """A PostgreSQL operation failed; the driver exception is chained as the cause."""


class RevisionConflict(PostgresStorageError):
    """A write carried ``expected_revision`` and the stored row had a different one.

    The message names no ids; ``collection``, ``scope``, ``key`` and ``expected_revision`` carry them.
    """

    def __init__(
        self,
        message: str,
        *,
        collection: str | None = None,
        scope: str | None = None,
        key: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.collection = collection
        self.scope = scope
        self.key = key
        self.expected_revision = expected_revision


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
        if not isinstance(self.schema, str) or not _SCHEMA.fullmatch(self.schema):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("schema must be a lowercase PostgreSQL identifier (letters, digits, underscores).")
        if self.schema.startswith("pg_"):
            raise ValueError("schema must not start with 'pg_'; PostgreSQL reserves that prefix for system schemas.")
        if not isinstance(self.prefix, str) or not _PREFIX.fullmatch(self.prefix):  # pyright: ignore[reportUnnecessaryIsInstance]
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
    ) -> None:
        if (connection_string is None) == (client is None):
            raise ValueError("Supply exactly one of connection_string or client.")
        if credential is not None and client is not None:
            raise ValueError(
                "credential cannot be combined with client; a pool or connection you pass signs in itself."
            )
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
            kwargs: dict[str, Any] = {"autocommit": True}
            if "connect_timeout" not in options:
                kwargs["connect_timeout"] = _CONNECT_TIMEOUT_SECONDS
            connection_class: type[AsyncConnection[Any]] = AsyncConnection
            if credential is not None:
                from ._entra import check_credential, entra_connection_class

                connection_class = entra_connection_class(credential)
                self._check_credential = functools.partial(check_credential, credential, conninfo, dict(kwargs))
            self.client = AsyncConnectionPool(
                conninfo,
                connection_class=connection_class,
                open=False,
                min_size=1,
                max_size=10,
                kwargs=kwargs,
                timeout=float(_CONNECT_TIMEOUT_SECONDS),
            )
        else:
            assert client is not None  # noqa: S101 - narrowed by the check above
            self.client = client
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
        return PostgresStorageError(message)

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
    async def connection(self) -> AsyncGenerator[AsyncConnection[Any]]:
        """Yield a connection inside a transaction; psycopg errors become ``PostgresStorageError``.

        A pool, owned or borrowed, is opened on demand (``open()`` is idempotent). A borrowed pool its
        owner already closed cannot be reopened; psycopg_pool's ``PoolClosed`` surfaces as
        ``PostgresStorageError``. A borrowed connection is held under a lock shared by every handle
        over it, so concurrent calls on one connection are serialized.
        """
        self._ensure_open()
        try:
            if isinstance(self.client, AsyncConnectionPool):
                # The handle that owns the pool opens it, so a hub's stores get its credential check too.
                await self._root().open()
                await self.client.open()
                async with self.client.connection() as connection, connection.transaction():
                    yield connection
            else:
                # Calls over one connection run one at a time. On a connection already inside the caller's
                # transaction this block is a savepoint, and transaction-scoped locks last until that ends.
                lock = _CONNECTION_LOCKS.setdefault(self.client, asyncio.Lock())
                async with lock, self.client.transaction():
                    yield self.client
        except PoolTimeout as exc:
            assert isinstance(self.client, AsyncConnectionPool)  # noqa: S101 - only a pool raises PoolTimeout
            raise self._no_connection(self.client) from exc
        except Error as exc:
            raise PostgresStorageError("PostgreSQL operation failed; see the chained driver exception.") from exc

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
) -> ClientHandle:
    """Resolve the connection the way every store does: explicit argument, ``.env`` file, environment.

    A ``ClientHandle`` (how ``PostgresPersistence`` shares its pool) is borrowed as a child handle, so
    closing the handle it came from stops this one too. ``credential`` signs the owned pool in with
    Microsoft Entra ID and needs a connection string, not a client.
    """
    if client is not None:
        if credential is not None:
            raise ValueError(
                "credential cannot be combined with client; a pool or connection you pass signs in itself."
            )
        if connection_string is not None or env_file_path is not None or env_file_encoding is not None:
            raise ValueError("client cannot be combined with connection_string, env_file_path or env_file_encoding.")
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
    return ClientHandle(resolved, None, credential=credential)
