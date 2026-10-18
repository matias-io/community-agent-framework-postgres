"""Retention: an optional time-to-live per store and the purge that acts on it."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Self, cast

from psycopg import sql

if TYPE_CHECKING:
    from ._client import ClientHandle

RetentionMode = Literal["tombstone", "delete"]

EXPIRES_AT = sql.SQL("CASE WHEN %(ttl)s::interval IS NULL THEN NULL ELSE now() + %(ttl)s::interval END")
"""Value for an ``expires_at`` column on write; pass ``ttl`` (``timedelta | None``) in the parameters."""


@dataclass(frozen=True)
class RetentionPolicy:
    """How long rows live after their last write and what ``purge`` does to expired ones.

    ``ttl`` of ``None`` (the default) means nothing ever expires. ``tombstone`` keeps
    the row with its ids and timestamps and drops the payload;
    ``delete`` removes the row. History messages and checkpoints are always deleted, and so are
    expired leases, which carry no policy.
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


class _ReadOnlyCounts(Mapping[str, int]):
    """A read-only mapping over a private ``dict``; unlike a ``dict`` subclass, no ``dict`` method can change it.

    It is built in ``__new__``, so calling ``__init__`` again changes nothing. ``deepcopy`` returns a plain
    ``dict``, which is what ``dataclasses.asdict`` puts under ``counts``; ``PurgeReport`` copies itself through
    its own ``__reduce__``, so a copied report stays read-only.
    """

    __slots__ = ("_data",)
    _data: dict[str, int]

    def __new__(cls, data: Mapping[str, int]) -> Self:
        self = super().__new__(cls)
        object.__setattr__(self, "_data", dict(data))
        return self

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise TypeError("PurgeReport.counts is read-only.")

    def __getitem__(self, key: str) -> int:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self._data == dict(cast("Mapping[object, object]", other))
        return NotImplemented

    __hash__ = None  # equal to a dict, so unhashable like one

    def __reduce__(self) -> tuple[type[_ReadOnlyCounts], tuple[dict[str, int]]]:
        return (_ReadOnlyCounts, (dict(self._data),))

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, int]:
        return dict(self._data)


@dataclass(frozen=True)
class PurgeReport:
    """Rows affected per table by one purge; ``counts`` is a read-only copy. ``sum(reports)`` works."""

    counts: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _ReadOnlyCounts(self.counts))

    def __reduce__(self) -> tuple[type[PurgeReport], tuple[dict[str, int]]]:
        # Pickle, copy and deepcopy rebuild the report from a plain dict, so the copy is read-only too.
        return (PurgeReport, (dict(self.counts),))

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
    client: ClientHandle,
    *,
    table: sql.Identifier,
    where: sql.Composable,
    params: Mapping[str, Any],
    mode: RetentionMode,
    tombstone: sql.Composable | None,
    batch_size: int = 1000,
) -> int:
    """Tombstone or delete the expired rows matching ``where``, ``batch_size`` rows per transaction.

    ``where`` may qualify columns with ``c``, the candidate row's alias. Rows another purge holds
    are skipped and taken by a later batch, so two purges never block each other.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    if mode == "tombstone" and tombstone is not None:
        candidates = sql.SQL(
            "SELECT c.ctid FROM {target} c WHERE ({predicate}) AND c.expires_at < now() AND c.purged_at IS NULL"
            " LIMIT %(batch)s FOR UPDATE SKIP LOCKED"
        )
        statement = sql.SQL("UPDATE {target} SET {cleared}, purged_at = now() WHERE ctid IN ({rows})")
    else:
        candidates = sql.SQL(
            "SELECT c.ctid FROM {target} c WHERE ({predicate}) AND c.expires_at < now()"
            " LIMIT %(batch)s FOR UPDATE SKIP LOCKED"
        )
        statement = sql.SQL("DELETE FROM {target} WHERE ctid IN ({rows})")
    composed = statement.format(
        target=table, cleared=tombstone or sql.SQL(""), rows=candidates.format(target=table, predicate=where)
    )
    total = 0
    while True:
        # READ COMMITTED: under a REPEATABLE READ default, SKIP LOCKED on a row changed after the snapshot
        # would raise a serialization failure instead of skipping it.
        async with client.connection(read_committed=True) as connection:
            cursor = await connection.execute(composed, dict(params) | {"batch": batch_size})
            count = cursor.rowcount
        total += count
        if count < batch_size:
            return total
