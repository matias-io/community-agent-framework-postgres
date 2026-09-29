"""``python -m agent_framework_community_postgres``: migrate, status, purge."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from agent_framework.exceptions import SettingNotFoundError

from ._client import PostgresStorageError, TableNames
from ._migrations import MIGRATIONS, render
from ._persistence import PostgresPersistence


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Add the shared options; subcommand copies suppress defaults so both positions work."""

    def d(value: str | None) -> str | None:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument(
        "--connection-string", default=d(None), help="psycopg conninfo or URI; defaults to POSTGRES_CONNECTION_STRING"
    )
    parser.add_argument("--schema", default=d("public"))
    parser.add_argument("--table-prefix", default=d("af_"))
    parser.add_argument(
        "--application-id", default=d(None), help="required by purge; migrate and status use 'cli' when omitted"
    )


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
    purge = commands.add_parser(
        "purge", parents=[shared], help="tombstone or delete the rows whose expires_at has passed"
    )
    purge.add_argument("--mode", choices=["tombstone", "delete"], default="tombstone")
    return parser


async def _run(args: argparse.Namespace) -> int:
    async with PostgresPersistence(
        application_id=args.application_id or "cli",
        connection_string=args.connection_string,
        schema=args.schema,
        table_prefix=args.table_prefix,
    ) as persistence:
        if args.command == "migrate":
            report = await persistence.migrate()
            print(f"applied: {', '.join(map(str, report.applied)) or 'none'}; current version: {report.current}")
        elif args.command == "status":
            pending = await persistence.pending_migrations()
            current = len(MIGRATIONS) if not pending else pending[0] - 1
            print(f"current version: {current}; pending: {', '.join(map(str, pending)) or 'none'}")
        else:
            report = await persistence.purge(mode=args.mode)
            for table, count in sorted(report.counts.items()):
                print(f"{table}: {count}")
            print(f"total: {report.total}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code (2 for invalid options, 1 for other errors)."""
    args = _parser().parse_args(argv)
    if args.command == "purge" and args.application_id is None:
        _parser().error("purge needs --application-id")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        names = TableNames(schema=args.schema, prefix=args.table_prefix)
        if args.command == "migrate" and args.print_sql:
            print(render(names), end="")
            return 0
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            return runner.run(_run(args))
    except ValueError as exc:
        # This package's option checks (TableNames, the connection string) raise ValueError with no values in it.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PostgresStorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
    except SettingNotFoundError:
        print("error: set POSTGRES_CONNECTION_STRING or pass --connection-string", file=sys.stderr)
    return 1
