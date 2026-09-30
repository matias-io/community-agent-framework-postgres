"""Numbered schema migrations. The list index plus one is the version, as in LangGraph's checkpointer."""

from __future__ import annotations

import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from psycopg import sql

from ._client import ClientHandle, PostgresStorageError, TableNames

Migration = Callable[[TableNames], Sequence[sql.Composed]]


def _v1(names: TableNames) -> list[sql.Composed]:
    t = names.table
    i = names.index
    return [
        sql.SQL(
            "CREATE TABLE {history} ("
            " id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " application_id text NOT NULL,"
            " tenant_id text NOT NULL DEFAULT '',"
            " agent_id text NOT NULL DEFAULT '',"
            " source_id text NOT NULL,"
            " session_id text NOT NULL,"
            " message jsonb NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " expires_at timestamptz)"
        ).format(history=t("history_messages")),
        sql.SQL(
            "CREATE INDEX {idx} ON {history} (application_id, tenant_id, agent_id, source_id, session_id, id)"
        ).format(idx=i("history_messages_session_idx"), history=t("history_messages")),
        sql.SQL("CREATE INDEX {idx} ON {history} (expires_at) WHERE expires_at IS NOT NULL").format(
            idx=i("history_messages_expires_idx"), history=t("history_messages")
        ),
        sql.SQL(
            "CREATE TABLE {sessions} ("
            " application_id text NOT NULL,"
            " session_id text NOT NULL,"
            " snapshot jsonb,"
            " snapshot_version text NOT NULL,"
            " revision bigint NOT NULL DEFAULT 1,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " expires_at timestamptz,"
            " purged_at timestamptz,"
            " PRIMARY KEY (application_id, session_id))"
        ).format(sessions=t("sessions")),
        sql.SQL(
            "CREATE TABLE {checkpoints} ("
            " application_id text NOT NULL,"
            " scope text NOT NULL DEFAULT '',"
            " workflow_name text NOT NULL,"
            " checkpoint_id text NOT NULL,"
            " previous_checkpoint_id text,"
            " checkpoint_timestamp timestamptz NOT NULL,"
            " iteration_count integer NOT NULL,"
            " encoded jsonb NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " expires_at timestamptz,"
            " PRIMARY KEY (application_id, scope, checkpoint_id))"
        ).format(checkpoints=t("checkpoints")),
        sql.SQL(
            "CREATE INDEX {idx} ON {checkpoints}"
            " (application_id, scope, workflow_name, checkpoint_timestamp, created_at)"
        ).format(idx=i("checkpoints_workflow_idx"), checkpoints=t("checkpoints")),
        sql.SQL(
            "CREATE TABLE {snapshots} ("
            " application_id text NOT NULL,"
            " scope text NOT NULL,"
            " thread_id text NOT NULL,"
            " messages jsonb,"
            " state jsonb,"
            " interrupt jsonb,"
            " session_state jsonb,"
            " revision bigint NOT NULL DEFAULT 1,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " expires_at timestamptz,"
            " purged_at timestamptz,"
            " PRIMARY KEY (application_id, scope, thread_id))"
        ).format(snapshots=t("thread_snapshots")),
        sql.SQL(
            "CREATE TABLE {documents} ("
            " application_id text NOT NULL,"
            " collection text NOT NULL,"
            " scope text NOT NULL,"
            " key text NOT NULL,"
            " payload jsonb,"
            " metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,"
            " revision bigint NOT NULL DEFAULT 1,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " expires_at timestamptz,"
            " purged_at timestamptz,"
            " PRIMARY KEY (application_id, collection, scope, key))"
        ).format(documents=t("documents")),
        sql.SQL("CREATE INDEX {idx} ON {documents} (application_id, collection, scope, updated_at DESC)").format(
            idx=i("documents_scope_idx"), documents=t("documents")
        ),
        sql.SQL(
            "CREATE TABLE {leases} ("
            " application_id text NOT NULL,"
            " resource text NOT NULL,"
            " owner text NOT NULL,"
            " token text NOT NULL,"
            " expires_at timestamptz NOT NULL,"
            " PRIMARY KEY (application_id, resource))"
        ).format(leases=t("leases")),
    ]


MIGRATIONS: tuple[Migration, ...] = (_v1,)


@dataclass(frozen=True)
class MigrationReport:
    """What ``migrate`` did: the versions it applied this run and the version now current."""

    applied: tuple[int, ...]
    current: int


def _version_table(names: TableNames) -> sql.Composed:
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {migrations} ("
        " version integer PRIMARY KEY,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    ).format(migrations=names.table("migrations"))


def render(names: TableNames, versions: Sequence[int] | None = None) -> str:
    """The SQL ``migrate`` would run, as text, for review or manual application by a DBA.

    Each version is one transaction (``BEGIN;`` ... ``COMMIT;``), as in ``migrate``.
    """
    chosen = list(range(1, len(MIGRATIONS) + 1)) if versions is None else list(versions)
    if any(not 1 <= version <= len(MIGRATIONS) for version in chosen):
        raise ValueError(f"versions must be between 1 and {len(MIGRATIONS)}.")
    parts: list[str] = []
    if chosen:
        parts.append(_version_table(names).as_string() + ";")
    for version in chosen:
        parts.append(f"-- version {version}")
        parts.append("BEGIN;")
        parts.extend(statement.as_string() + ";" for statement in MIGRATIONS[version - 1](names))
        parts.append(
            sql.SQL("INSERT INTO {migrations} (version) VALUES ({version})")  # noqa: S608
            .format(migrations=names.table("migrations"), version=sql.Literal(version))
            .as_string()
            + ";"
        )
        parts.append("COMMIT;")
    return "\n".join(parts) + ("\n" if parts else "")


async def current_version(client: ClientHandle, names: TableNames) -> int:
    """The highest applied version, or 0 when nothing has been applied."""
    async with client.connection() as connection:
        cursor = await connection.execute("SELECT to_regclass(%s)", (names.qualified("migrations"),))
        found = await cursor.fetchone()
        if found is None or found[0] is None:
            return 0
        cursor = await connection.execute(
            sql.SQL("SELECT coalesce(max(version), 0) FROM {migrations}").format(migrations=names.table("migrations"))
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


def ensure_supported(current: int) -> None:
    """Raise ``PostgresStorageError`` when the database was migrated by a newer release of this package."""
    if current > len(MIGRATIONS):
        raise PostgresStorageError(
            f"Database schema version {current} is newer than this package supports ({len(MIGRATIONS)});"
            " upgrade the package."
        )


async def pending_versions(client: ClientHandle, names: TableNames) -> list[int]:
    """Versions ``migrate`` would apply now; raise when the database is newer than this package."""
    current = await current_version(client, names)
    ensure_supported(current)
    return list(range(current + 1, len(MIGRATIONS) + 1))


async def migrate(client: ClientHandle, names: TableNames) -> MigrationReport:
    """Apply every pending migration, one transaction each, serialized across processes by an advisory lock."""
    # A crc32 collision between two schemas' keys only makes their migrations wait for each other.
    lock_key = zlib.crc32(names.qualified("migrations").encode("utf-8"))
    applied: list[int] = []
    async with client.connection() as connection:
        # The lock also covers the CREATE TABLE IF NOT EXISTS, which races on pg_type when two processes start fresh.
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        await connection.execute(_version_table(names))
    for version in await pending_versions(client, names):
        async with client.connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cursor = await connection.execute(
                sql.SQL("SELECT 1 FROM {migrations} WHERE version = %s").format(migrations=names.table("migrations")),
                (version,),
            )
            if await cursor.fetchone() is not None:
                continue  # another process applied it while we waited for the lock
            for statement in MIGRATIONS[version - 1](names):
                await connection.execute(statement)
            await connection.execute(
                sql.SQL("INSERT INTO {migrations} (version) VALUES (%s)").format(migrations=names.table("migrations")),
                (version,),
            )
            applied.append(version)
    return MigrationReport(applied=tuple(applied), current=await current_version(client, names))
