"""``python -m agent_framework_community_postgres``: migrate, status, purge."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta

from ._client import TableNames
from ._migrations import MIGRATIONS, render
from ._persistence import PostgresPersistence
from ._retention import RetentionPolicy


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Add the shared options; subcommand copies suppress defaults so both positions work."""

    def d(value: str | None) -> str | None:
        return argparse.SUPPRESS if suppress else value  # type: ignore[return-value]

    parser.add_argument(
        "--connection-string", default=d(None), help="psycopg conninfo or URI; defaults to POSTGRES_CONNECTION_STRING"
    )
    parser.add_argument("--schema", default=d("public"))
    parser.add_argument("--table-prefix", default=d("af_"))
    parser.add_argument("--application-id", default=d("cli"), help="required by purge; ignored by migrate and status")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_framework_community_postgres",
        description="Manage the PostgreSQL schema used by community-agent-framework-postgres.",
    )
    _add_common(parser)
    shared = argparse.ArgumentParser(add_help=False)
    _add_common(shared, suppress=True)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate", parents=[shared], help="apply pending migrations")
    migrate.add_argument("--print", action="store_true", dest="print_sql", help="print the SQL instead of running it")
    commands.add_parser("status", parents=[shared], help="show the applied and pending versions")
    purge = commands.add_parser("purge", parents=[shared], help="tombstone or delete rows older than --ttl seconds")
    purge.add_argument("--ttl", type=int, required=True, help="seconds since the last write after which rows expire")
    purge.add_argument("--mode", choices=["tombstone", "delete"], default="tombstone")
    return parser


async def _run(args: argparse.Namespace) -> int:
    retention = RetentionPolicy(ttl=timedelta(seconds=args.ttl), mode=args.mode) if args.command == "purge" else None
    async with PostgresPersistence(
        application_id=args.application_id,
        connection_string=args.connection_string,
        schema=args.schema,
        table_prefix=args.table_prefix,
        retention=retention,
    ) as persistence:
        if args.command == "migrate":
            report = await persistence.migrate()
            print(f"applied: {', '.join(map(str, report.applied)) or 'none'}; current version: {report.current}")
        elif args.command == "status":
            pending = await persistence.pending_migrations()
            current = len(MIGRATIONS) if not pending else pending[0] - 1
            print(f"current version: {current}; pending: {', '.join(map(str, pending)) or 'none'}")
        else:
            report = await persistence.purge()
            for table, count in sorted(report.counts.items()):
                print(f"{table}: {count}")
            print(f"total: {report.total}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "migrate" and args.print_sql:
        print(render(TableNames(schema=args.schema, prefix=args.table_prefix)), end="")
        return 0
    if args.command == "purge" and args.application_id == "cli":
        _parser().error("purge needs --application-id")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(_run(args))
