"""Workflow checkpoints in PostgreSQL with the framework's own hybrid JSON + restricted-pickle encoding."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from agent_framework import SecretString, WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql

from ._client import ClientHandle, PostgresClient, optional_text
from ._entra import EntraCredential
from ._framework import decode_checkpoint_value, encode_checkpoint_value
from ._json import encode_jsonb
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore

logger = logging.getLogger(__name__)


class PostgresCheckpointStorage(BaseStore):
    """PostgreSQL-backed workflow ``CheckpointStorage``.

    Checkpoints are encoded exactly as ``FileCheckpointStorage`` and ``CosmosCheckpointStorage``
    encode them, so the same restricted deserializer and ``allowed_checkpoint_types`` apply, and
    the same warning: the table is a trust boundary, because non-JSON values are pickled. Rows are
    keyed by application, optional ``tenant_id`` and ``scope`` (a conversation or response id, the
    way Foundry hosting scopes its stores) and the checkpoint id. "Latest" is the last row written
    in a scope, whatever clock produced it. Saving an existing id replaces its content and keeps
    its position, as the framework's own stores do.
    """

    PAYLOAD_FORMAT: ClassVar[str] = "agent-framework.python.checkpoint.v1"

    def __init__(
        self,
        *,
        application_id: str,
        tenant_id: str | None = None,
        scope: str | None = None,
        allowed_checkpoint_types: list[str] | None = None,
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
        super().__init__(
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
        self.tenant_id = optional_text(tenant_id, "tenant_id")
        self.scope = optional_text(scope, "scope")
        self._allowed_types: frozenset[str] = frozenset(allowed_checkpoint_types or [])
        self._lock_key = json.dumps(
            [
                "agent-framework-postgres:checkpoints",
                schema,
                table_prefix,
                self.application_id,
                self.tenant_id,
                self.scope,
            ]
        )

    _SCOPE = sql.SQL(
        "application_id = %(app)s AND tenant_id = %(tenant)s AND scope = %(scope)s AND payload_format = %(format)s"
    )

    def _params(self) -> dict[str, Any]:
        return {
            "app": self.application_id,
            "tenant": self.tenant_id,
            "scope": self.scope,
            "format": self.PAYLOAD_FORMAT,
        }

    @staticmethod
    def _parse_timestamp(checkpoint: WorkflowCheckpoint) -> datetime:
        try:
            parsed = datetime.fromisoformat(checkpoint.timestamp)
        except (TypeError, ValueError) as exc:
            raise WorkflowCheckpointException("A checkpoint has an unparsable timestamp.") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _decode(self, payload: Any) -> WorkflowCheckpoint:
        try:
            return WorkflowCheckpoint.from_dict(decode_checkpoint_value(payload, allowed_types=self._allowed_types))
        except WorkflowCheckpointException:
            raise
        except Exception as exc:
            raise WorkflowCheckpointException("A stored checkpoint cannot be restored.") from exc

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        """Encode, verify it can be restored under this store's allowed types, then write it."""
        when = self._parse_timestamp(checkpoint)
        try:
            encoded = encode_checkpoint_value(checkpoint.to_dict())
            decode_checkpoint_value(encoded, allowed_types=self._allowed_types)
        except WorkflowCheckpointException:
            raise
        except Exception as exc:
            raise WorkflowCheckpointException(
                "A checkpoint cannot be encoded or restored under this storage's allowed types."
            ) from exc
        payload = encode_jsonb(encoded, what="Checkpoint")
        async with self._client.connection(read_committed=True) as connection:
            # Writers of one scope take turns, so sequence order is commit order.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (self._lock_key,))
            await connection.execute(
                sql.SQL(
                    "INSERT INTO {checkpoints} (application_id, tenant_id, scope, payload_format, workflow_name,"
                    " checkpoint_id, previous_checkpoint_id, checkpoint_timestamp, payload, expires_at)"
                    " VALUES (%(app)s, %(tenant)s, %(scope)s, %(format)s, %(workflow)s, %(id)s, %(previous)s,"
                    " %(when)s, %(payload)s, {expires})"
                    " ON CONFLICT (application_id, tenant_id, scope, checkpoint_id) DO UPDATE SET"
                    " workflow_name = EXCLUDED.workflow_name, previous_checkpoint_id = EXCLUDED.previous_checkpoint_id,"
                    " checkpoint_timestamp = EXCLUDED.checkpoint_timestamp, payload = EXCLUDED.payload,"
                    " expires_at = EXCLUDED.expires_at, payload_format = EXCLUDED.payload_format"
                ).format(checkpoints=self._names.table("checkpoints"), expires=EXPIRES_AT),
                self._params()
                | {
                    "workflow": checkpoint.workflow_name,
                    "id": checkpoint.checkpoint_id,
                    "previous": checkpoint.previous_checkpoint_id,
                    "when": when,
                    "payload": payload,
                    "ttl": self._ttl(),
                },
            )
        logger.debug("Saved a checkpoint.")
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        """The checkpoint with this id in this scope; ``WorkflowCheckpointException`` when absent or unreadable."""
        async with self._client.connection(transactional=False) as connection:
            cursor = await connection.execute(
                sql.SQL("SELECT payload FROM {checkpoints} WHERE {scope} AND checkpoint_id = %(id)s").format(
                    checkpoints=self._names.table("checkpoints"), scope=self._SCOPE
                ),
                self._params() | {"id": checkpoint_id},
            )
            row = await cursor.fetchone()
        if row is None:
            raise WorkflowCheckpointException("No checkpoint found with that ID in this scope.")
        return self._decode(row[0])

    async def _rows(
        self, workflow_name: str, *, column: sql.Identifier, newest_first: bool = False, limit: int | None = None
    ) -> list[Any]:
        order = sql.SQL("DESC") if newest_first else sql.SQL("ASC")
        async with self._client.connection(transactional=False) as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT {column} FROM {checkpoints} WHERE {scope} AND workflow_name = %(workflow)s"
                    " ORDER BY sequence {order} LIMIT %(limit)s"
                ).format(column=column, checkpoints=self._names.table("checkpoints"), scope=self._SCOPE, order=order),
                self._params() | {"workflow": workflow_name, "limit": limit},
            )
            return [row[0] for row in await cursor.fetchall()]

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        """Checkpoints for the workflow in write order; rows that fail to decode are logged and skipped."""
        found: list[WorkflowCheckpoint] = []
        for payload in await self._rows(workflow_name, column=sql.Identifier("payload")):
            try:
                found.append(self._decode(payload))
            except WorkflowCheckpointException:
                logger.warning("Skipping a checkpoint row that failed to decode.")
        return found

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        """Every checkpoint id for the workflow in write order, without decoding any row."""
        return await self._rows(workflow_name, column=sql.Identifier("checkpoint_id"))

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        """The last checkpoint written for the workflow in this scope, or ``None``; raises when it does not decode."""
        rows = await self._rows(workflow_name, column=sql.Identifier("payload"), newest_first=True, limit=1)
        return self._decode(rows[0]) if rows else None

    async def delete(self, checkpoint_id: str) -> bool:
        """Delete by id within this store's scope; ``True`` when a row existed."""
        async with self._client.connection(read_committed=True) as connection:
            cursor = await connection.execute(
                sql.SQL("DELETE FROM {checkpoints} WHERE {scope} AND checkpoint_id = %(id)s").format(
                    checkpoints=self._names.table("checkpoints"), scope=self._SCOPE
                ),
                self._params() | {"id": checkpoint_id},
            )
            return cursor.rowcount > 0

    async def purge(self) -> PurgeReport:
        """Delete expired checkpoints in this store's scope, whatever policy stamped them (no tombstone form)."""
        count = await purge_rows(
            self._client,
            table=self._names.table("checkpoints"),
            where=sql.SQL("application_id = %(app)s AND tenant_id = %(tenant)s AND scope = %(scope)s"),
            params={"app": self.application_id, "tenant": self.tenant_id, "scope": self.scope},
            mode="delete",
            tombstone=None,
        )
        return PurgeReport({f"{self._names.prefix}checkpoints": count})
