"""PostgreSQL storage for Microsoft Agent Framework.

Every store can be built on its own, or from one :class:`PostgresPersistence`
that shares a single connection pool. See the README for examples.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from ._checkpoint_storage import PostgresCheckpointStorage
from ._client import (
    LeaseLost,
    LeaseUnavailable,
    PostgresClient,
    PostgresSettings,
    PostgresStorageError,
    RevisionConflict,
    TableNames,
)
from ._document_store import Document, DocumentSummary, PostgresDocumentStore
from ._history_provider import PostgresHistoryProvider
from ._leases import Lease, PostgresLeases
from ._migrations import MIGRATIONS, MigrationReport
from ._migrations import render as render_migrations
from ._persistence import PostgresPersistence
from ._retention import PurgeReport, RetentionMode, RetentionPolicy
from ._session_store import PostgresSessionStore

if TYPE_CHECKING:
    from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore

try:
    __version__ = version("community-agent-framework-postgres")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"

_LAZY = {"PostgresAGUIThreadSnapshotStore"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from ._thread_snapshot_store import PostgresAGUIThreadSnapshotStore

        return PostgresAGUIThreadSnapshotStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


__all__ = [
    "MIGRATIONS",
    "Document",
    "DocumentSummary",
    "Lease",
    "LeaseLost",
    "LeaseUnavailable",
    "MigrationReport",
    "PostgresAGUIThreadSnapshotStore",
    "PostgresCheckpointStorage",
    "PostgresClient",
    "PostgresDocumentStore",
    "PostgresHistoryProvider",
    "PostgresLeases",
    "PostgresPersistence",
    "PostgresSessionStore",
    "PostgresSettings",
    "PostgresStorageError",
    "PurgeReport",
    "RetentionMode",
    "RetentionPolicy",
    "RevisionConflict",
    "TableNames",
    "__version__",
    "render_migrations",
]
