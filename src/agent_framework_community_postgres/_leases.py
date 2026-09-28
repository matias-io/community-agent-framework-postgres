"""Short, renewable, owner-tagged leases: the one coordination primitive this package offers."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta

from agent_framework import SecretString
from psycopg import sql

from ._client import ClientHandle, LeaseLost, LeaseUnavailable, PostgresClient, require_text
from ._retention import RetentionPolicy
from ._store import BaseStore

_FIRST_DELAY = 0.1
_MAX_DELAY = 2.0


class Lease:
    """A lease the caller holds; ``renew`` extends it, ``release`` gives it up.

    ``token`` is new on every acquisition, so two holders with the same ``owner`` never
    renew or release each other's lease.
    """

    def __init__(self, store: PostgresLeases, resource: str, owner: str, ttl: timedelta, token: str) -> None:
        self._store = store
        self.resource = resource
        self.owner = owner
        self.ttl = ttl
        self.token = token

    async def renew(self) -> None:
        """Extend the lease by ``ttl`` from now; raise ``LeaseLost`` if it expired or changed hands."""
        await self._store._renew(self)  # pyright: ignore[reportPrivateUsage]

    async def release(self) -> None:
        """Give the lease up. Releasing a lease that is no longer ours does nothing."""
        await self._store._release(self)  # pyright: ignore[reportPrivateUsage]


class PostgresLeases(BaseStore):
    """Leases keyed by ``(application_id, resource)``, judged by the database clock.

    Acquisition is one upsert that succeeds only when the row is absent or expired, so
    there is one holder per acquisition, whatever ``owner`` says; a holder that crashed
    is recovered when its lease expires. No advisory locks are used, so a connection is
    never pinned while the caller does slow work.
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
    ) -> None:
        super().__init__(
            application_id=application_id,
            connection_string=connection_string,
            client=client,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            schema=schema,
            table_prefix=table_prefix,
            retention=RetentionPolicy(),
        )

    async def try_acquire(self, resource: str, *, owner: str, ttl: timedelta) -> Lease | None:
        """Take the lease if it is free or expired; ``None`` while anyone, this owner included, holds it."""
        require_text(resource, "resource")
        require_text(owner, "owner")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive.")
        token = uuid.uuid4().hex
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "INSERT INTO {leases} (application_id, resource, owner, token, expires_at)"
                    " VALUES (%(app)s, %(resource)s, %(owner)s, %(token)s, now() + %(ttl)s)"
                    " ON CONFLICT (application_id, resource) DO UPDATE"
                    " SET owner = EXCLUDED.owner, token = EXCLUDED.token, expires_at = EXCLUDED.expires_at"
                    " WHERE {leases}.expires_at < now()"
                    " RETURNING token"
                ).format(leases=self._names.table("leases")),
                {"app": self.application_id, "resource": resource, "owner": owner, "token": token, "ttl": ttl},
            )
            row = await cursor.fetchone()
        return Lease(self, resource, owner, ttl, token) if row is not None else None

    @asynccontextmanager
    async def acquire(
        self, resource: str, *, owner: str, ttl: timedelta, wait: timedelta = timedelta(0)
    ) -> AsyncGenerator[Lease]:
        """Hold the lease for the block, waiting up to ``wait`` for it to free up; release on exit."""
        deadline = time.monotonic() + wait.total_seconds()
        delay = _FIRST_DELAY
        while True:
            lease = await self.try_acquire(resource, owner=owner, ttl=ttl)
            if lease is not None:
                break
            if time.monotonic() >= deadline:
                raise LeaseUnavailable(f"Lease on {resource!r} is held by another owner.")
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, _MAX_DELAY)
        try:
            yield lease
        finally:
            await lease.release()

    def _lease_params(self, lease: Lease) -> dict[str, object]:
        return {"app": self.application_id, "resource": lease.resource, "owner": lease.owner, "token": lease.token}

    async def _renew(self, lease: Lease) -> None:
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "UPDATE {leases} SET expires_at = now() + %(ttl)s"
                    " WHERE application_id = %(app)s AND resource = %(resource)s AND owner = %(owner)s"
                    " AND token = %(token)s AND expires_at >= now() RETURNING owner"
                ).format(leases=self._names.table("leases")),
                self._lease_params(lease) | {"ttl": lease.ttl},
            )
            if await cursor.fetchone() is None:
                raise LeaseLost(f"Lease on {lease.resource!r} expired or was taken over.")

    async def _release(self, lease: Lease) -> None:
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL(
                    "DELETE FROM {leases}"
                    " WHERE application_id = %(app)s AND resource = %(resource)s AND owner = %(owner)s"
                    " AND token = %(token)s"
                ).format(leases=self._names.table("leases")),
                self._lease_params(lease),
            )
