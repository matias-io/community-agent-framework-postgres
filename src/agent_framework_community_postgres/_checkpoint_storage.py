"""Workflow checkpoints in PostgreSQL with the framework's own hybrid JSON + restricted-pickle encoding."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from agent_framework import SecretString, WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql

from ._client import ClientHandle, PostgresClient, optional_text
from ._framework import decode_checkpoint_value, encode_checkpoint_value
from ._json import encode_jsonb
from ._retention import EXPIRES_AT, PurgeReport, RetentionPolicy, purge_rows
from ._store import BaseStore

logger = logging.getLogger(__name__)


class PostgresCheckpointStorage(BaseStore):
    """PostgreSQL-backed workflow ``CheckpointStorage``.

    Checkpoints are encoded exactly as ``FileCheckpointStorage`` and
    ``CosmosCheckpointStorage`` encode them, so the same restricted deserializer and
    ``allowed_checkpoint_types`` rules apply, and the same warning: the database is a
    trust boundary because non-JSON values are pickled. A ``scope`` (for example a
    conversation id) keeps one workflow's checkpoints apart per conversation; MAF's
    protocol only knows ``workflow_name``.
    """

    def __init__(
        self,
        *,
        application_id: str,
        scope: str | None = None,
        allowed_checkpoint_types: list[str] | None = None,
        connection_string: str | SecretString | None = None,
        client: PostgresClient | ClientHandle | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
        schema: str = "public",
        table_prefix: str = "af_",
        retention: RetentionPolicy | None = None,
    ) -> None:
        super().__init__(
            application_id=application_id,
            connection_string=connection_string,
            client=client,
            env_file_path=env_file_path,
            env_file_encoding=env_file_encoding,
            schema=schema,
            table_prefix=table_prefix,
            retention=retention,
        )
        self.scope = optional_text(scope, "scope")
        self._allowed_types: frozenset[str] = frozenset(allowed_checkpoint_types or [])

    def _base(self) -> dict[str, Any]:
        return {"app": self.application_id, "scope": self.scope}

    @staticmethod
    def _parse_timestamp(checkpoint: WorkflowCheckpoint) -> datetime:
        try:
            parsed = datetime.fromisoformat(checkpoint.timestamp)
        except (TypeError, ValueError) as exc:
            raise WorkflowCheckpointException(
                f"Checkpoint {checkpoint.checkpoint_id} has an unparsable timestamp {checkpoint.timestamp!r}."
            ) from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _decode(self, encoded: Any) -> WorkflowCheckpoint:
        decoded = decode_checkpoint_value(encoded, allowed_types=self._allowed_types)
        return WorkflowCheckpoint.from_dict(decoded)

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        """Encode, verify it can be restored under this store's allowed types, then upsert."""
        when = self._parse_timestamp(checkpoint)
        try:
            encoded = encode_checkpoint_value(checkpoint.to_dict())
            decode_checkpoint_value(encoded, allowed_types=self._allowed_types)
        except WorkflowCheckpointException:
            raise
        except Exception as exc:
            raise WorkflowCheckpointException(
                f"Checkpoint {checkpoint.checkpoint_id} cannot be encoded or restored"
                " under this storage's allowed types."
            ) from exc
        adapted = encode_jsonb(encoded, what="Checkpoint")
        async with self._client.connection() as connection:
            await connection.execute(
                sql.SQL(
                    "INSERT INTO {checkpoints} (application_id, scope, workflow_name, checkpoint_id,"
                    " previous_checkpoint_id, checkpoint_timestamp, iteration_count, encoded, expires_at)"
                    " VALUES (%(app)s, %(scope)s, %(workflow)s, %(id)s, %(previous)s, %(when)s, %(iteration)s,"
                    " %(encoded)s, {expires})"
                    " ON CONFLICT (application_id, scope, checkpoint_id) DO UPDATE SET"
                    " workflow_name = EXCLUDED.workflow_name, previous_checkpoint_id = EXCLUDED.previous_checkpoint_id,"
                    " checkpoint_timestamp = EXCLUDED.checkpoint_timestamp, iteration_count = EXCLUDED.iteration_count,"
                    " encoded = EXCLUDED.encoded, expires_at = EXCLUDED.expires_at"
                ).format(checkpoints=self._names.table("checkpoints"), expires=EXPIRES_AT),
                self._base()
                | {
                    "workflow": checkpoint.workflow_name,
                    "id": checkpoint.checkpoint_id,
                    "previous": checkpoint.previous_checkpoint_id,
                    "when": when,
                    "iteration": checkpoint.iteration_count,
                    "encoded": adapted,
                    "ttl": self._ttl(),
                },
            )
        logger.debug("Saved checkpoint for workflow %s", checkpoint.workflow_name)
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        """The checkpoint with this id in this store's scope; ``WorkflowCheckpointException`` when absent."""
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT encoded FROM {checkpoints}"
                    " WHERE application_id = %(app)s AND scope = %(scope)s AND checkpoint_id = %(id)s"
                ).format(checkpoints=self._names.table("checkpoints")),
                self._base() | {"id": checkpoint_id},
            )
            row = await cursor.fetchone()
        if row is None:
            raise WorkflowCheckpointException(f"No checkpoint found with ID {checkpoint_id}")
        return self._decode(row[0])

    async def _rows(self, workflow_name: str, *, newest_first: bool = False, limit: int | None = None) -> list[Any]:
        order = sql.SQL("DESC") if newest_first else sql.SQL("ASC")
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT checkpoint_id, encoded FROM {checkpoints}"
                    " WHERE application_id = %(app)s AND scope = %(scope)s AND workflow_name = %(workflow)s"
                    " ORDER BY checkpoint_timestamp {order}, created_at {order} LIMIT %(limit)s"
                ).format(checkpoints=self._names.table("checkpoints"), order=order),
                self._base() | {"workflow": workflow_name, "limit": limit},
            )
            return await cursor.fetchall()

    def _decoded(self, rows: list[Any]) -> Iterator[WorkflowCheckpoint]:
        """Decode rows in order, logging and skipping any that fail (MAF's ``FileCheckpointStorage`` does the same)."""
        for _checkpoint_id, encoded in rows:
            try:
                yield self._decode(encoded)
            except Exception:
                logger.warning("Skipping a checkpoint row that failed to decode.")

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        """Checkpoints for the workflow, oldest first; rows that fail to decode are logged and skipped."""
        return list(self._decoded(await self._rows(workflow_name)))

    async def delete(self, checkpoint_id: str) -> bool:
        """Delete by id within this store's scope; ``True`` when a row existed."""
        async with self._client.connection() as connection:
            cursor = await connection.execute(
                sql.SQL(
                    "DELETE FROM {checkpoints}"
                    " WHERE application_id = %(app)s AND scope = %(scope)s AND checkpoint_id = %(id)s"
                ).format(checkpoints=self._names.table("checkpoints")),
                self._base() | {"id": checkpoint_id},
            )
            return cursor.rowcount > 0

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        """The newest checkpoint that decodes, by its own timestamp, then by insertion time."""
        newest = await self._rows(workflow_name, newest_first=True, limit=1)
        if not newest:
            return None
        found = next(self._decoded(newest), None)
        if found is not None:
            return found
        # The newest row is undecodable (already logged): walk the rest, newest first.
        return next(self._decoded((await self._rows(workflow_name, newest_first=True))[1:]), None)

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        """Ids of the checkpoints that decode, oldest first; the others are logged and skipped."""
        return [checkpoint.checkpoint_id for checkpoint in await self.list_checkpoints(workflow_name=workflow_name)]

    async def purge(self) -> PurgeReport:
        """Delete expired checkpoints in this store's scope, whatever policy stamped them (no tombstone form)."""
        async with self._client.connection() as connection:
            count = await purge_rows(
                connection,
                table=self._names.table("checkpoints"),
                where=sql.SQL("application_id = %(app)s AND scope = %(scope)s"),
                params=self._base(),
                mode="delete",
                tombstone=None,
            )
        return PurgeReport({f"{self._names.prefix}checkpoints": count})
