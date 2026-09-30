"""Retention: an optional time-to-live per store and the purge that acts on it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Literal

from psycopg import AsyncConnection, sql

RetentionMode = Literal["tombstone", "delete"]

EXPIRES_AT = sql.SQL("CASE WHEN %(ttl)s::interval IS NULL THEN NULL ELSE now() + %(ttl)s::interval END")
"""Value for an ``expires_at`` column on write; pass ``ttl`` (``timedelta | None``) in the parameters."""


@dataclass(frozen=True)
class RetentionPolicy:
    """How long rows live after their last write and what ``purge`` does to expired ones.

    ``ttl`` of ``None`` (the default) means nothing ever expires. ``tombstone`` keeps
    the row with its ids, timestamps, revision and metadata and drops the payload;
    ``delete`` removes the row. History messages and checkpoints are always deleted.
    """

    ttl: timedelta | None = None
    mode: RetentionMode = "tombstone"

    def __post_init__(self) -> None:
        if self.ttl is not None and self.ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta or None.")
        if self.mode not in ("tombstone", "delete"):
            raise ValueError("mode must be 'tombstone' or 'delete'.")

    @property
    def enabled(self) -> bool:
        """Whether writes set ``expires_at``."""
        return self.ttl is not None


@dataclass(frozen=True)
class PurgeReport:
    """Rows affected per table by one purge; ``counts`` is a read-only copy. ``sum(reports)`` works."""

    counts: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __add__(self, other: PurgeReport) -> PurgeReport:
        merged = dict(self.counts)
        for table, count in other.counts.items():
            merged[table] = merged.get(table, 0) + count
        return PurgeReport(merged)

    def __radd__(self, other: int) -> PurgeReport:
        if other == 0:  # the start value of sum()
            return self
        return NotImplemented


async def purge_rows(
    connection: AsyncConnection[Any],
    *,
    table: sql.Identifier,
    where: sql.Composable,
    params: Mapping[str, Any],
    mode: RetentionMode,
    tombstone: sql.Composable | None,
) -> int:
    """Tombstone or delete the expired rows matching ``where``; return the count."""
    if mode == "tombstone" and tombstone is not None:
        statement = sql.SQL(
            "UPDATE {table} SET {tombstone}, purged_at = now() WHERE ({where})"
            " AND expires_at < now() AND purged_at IS NULL"
        ).format(table=table, tombstone=tombstone, where=where)
    else:
        statement = sql.SQL("DELETE FROM {table} WHERE ({where}) AND expires_at < now()").format(
            table=table, where=where
        )
    cursor = await connection.execute(statement, dict(params))
    return cursor.rowcount
