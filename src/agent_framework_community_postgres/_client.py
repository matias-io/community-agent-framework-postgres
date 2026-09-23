"""Connection ownership, settings, table naming and this package's error types."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeAlias, TypedDict

from agent_framework import SecretString, load_settings
from agent_framework.exceptions import IntegrationException
from psycopg import AsyncConnection, Error, sql
from psycopg_pool import AsyncConnectionPool

PostgresClient: TypeAlias = AsyncConnection[Any] | AsyncConnectionPool[AsyncConnection[Any]]

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
_PREFIX = re.compile(r"[a-z0-9_]*")
# The longest table or index suffix this package creates; a migration adding a longer one must update it.
_LONGEST_IDENTIFIER = "history_messages_session_idx"
_MAX_IDENTIFIER_BYTES = 63


class PostgresSettings(TypedDict, total=False):
    """Connection settings resolved from an explicit value, a selected ``.env`` file, or ``POSTGRES_*``."""

    connection_string: SecretString | None
    """Psycopg conninfo or URI; from ``POSTGRES_CONNECTION_STRING`` when not given explicitly."""


class PostgresStorageError(IntegrationException):
    """A PostgreSQL operation failed; the driver exception is chained as the cause."""


class RevisionConflict(PostgresStorageError):
    """A write carried ``expected_revision`` and the stored row had a different one."""


class LeaseUnavailable(PostgresStorageError):
    """Another owner holds the lease and the wait budget ran out."""


class LeaseLost(PostgresStorageError):
    """The lease expired or was taken over before ``renew`` or ``release`` ran."""


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
    """Own a lazily opened pool, or borrow a caller's pool or connection without closing it."""

    def __init__(self, connection_string: SecretString | None, client: PostgresClient | None) -> None:
        if (connection_string is None) == (client is None):
            raise ValueError("Supply exactly one of connection_string or client.")
        if client is not None and not isinstance(client, (AsyncConnection, AsyncConnectionPool)):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("client must be a psycopg AsyncConnection or AsyncConnectionPool.")
        self.owned = client is None
        self.client: PostgresClient
        if connection_string is not None:
            conninfo = connection_string.get_secret_value()
            if not conninfo.strip():
                raise ValueError("connection_string must not be empty.")
            self.client = AsyncConnectionPool(
                conninfo, open=False, min_size=1, max_size=10, kwargs={"autocommit": True}
            )
        else:
            assert client is not None  # noqa: S101 - narrowed by the check above
            self.client = client
        self.closed = False

    def __repr__(self) -> str:
        kind = type(self.client).__name__
        return f"ClientHandle(owned={self.owned}, client={kind}, closed={self.closed})"

    async def open(self) -> None:
        """Open an owned pool; a no-op for a borrowed client."""
        self._ensure_open()
        if self.owned and isinstance(self.client, AsyncConnectionPool):
            await self.client.open()

    def _ensure_open(self) -> None:
        if self.closed:
            raise PostgresStorageError("The Postgres client is closed.")

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection[Any]]:
        """Yield a connection inside a transaction; psycopg errors become ``PostgresStorageError``.

        A pool, owned or borrowed, is opened on demand (``open()`` is idempotent). A borrowed pool its
        owner already closed cannot be reopened; psycopg_pool's ``PoolClosed`` surfaces as
        ``PostgresStorageError``.
        """
        self._ensure_open()
        try:
            if isinstance(self.client, AsyncConnectionPool):
                await self.client.open()
                async with self.client.connection() as connection, connection.transaction():
                    yield connection
            else:
                # On a borrowed connection already in a transaction this becomes a savepoint.
                async with self.client.transaction():
                    yield self.client
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
    client: PostgresClient | None,
    env_file_path: str | None,
    env_file_encoding: str | None,
) -> ClientHandle:
    """Resolve the connection the way every store does: explicit argument, ``.env`` file, environment."""
    if client is not None:
        if connection_string is not None or env_file_path is not None or env_file_encoding is not None:
            raise ValueError("client cannot be combined with connection_string, env_file_path or env_file_encoding.")
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
    return ClientHandle(resolved, None)
