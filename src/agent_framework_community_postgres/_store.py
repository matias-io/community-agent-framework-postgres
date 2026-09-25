"""What every store shares: one client, one naming scheme, one retention policy."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Self

from agent_framework import SecretString

from ._client import (
    ClientHandle,
    PostgresClient,
    TableNames,
    create_client,
    require_text,
)
from ._retention import RetentionPolicy


class BaseStore:
    def __init__(
        self,
        *,
        application_id: str,
        connection_string: str | SecretString | None,
        client: PostgresClient | ClientHandle | None,
        env_file_path: str | None,
        env_file_encoding: str | None,
        schema: str,
        table_prefix: str,
        retention: RetentionPolicy | None,
    ) -> None:
        self.application_id = require_text(application_id, "application_id")
        self._names = TableNames(schema=schema, prefix=table_prefix)
        self._client: ClientHandle = create_client(
            connection_string, client=client, env_file_path=env_file_path, env_file_encoding=env_file_encoding
        )
        self._retention = retention or RetentionPolicy()

    @property
    def retention(self) -> RetentionPolicy:
        return self._retention

    def _ttl(self) -> timedelta | None:
        return self._retention.ttl

    async def close(self) -> None:
        """Close the pool this store created; a borrowed client is left alone."""
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()
