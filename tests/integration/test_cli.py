import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from psycopg import Connection, sql

from agent_framework_community_postgres._cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def schema(test_dsn: str) -> Iterator[str]:
    """Sync override of the shared fixture: these tests call ``main`` synchronously, outside any running loop."""
    name = f"cafp_test_{uuid4().hex[:12]}"
    with Connection.connect(test_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def test_status_migrate_status(test_dsn: str, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--connection-string", test_dsn, "--schema", schema]
    assert main(["status", *common]) == 0
    assert "current version: 0" in capsys.readouterr().out
    assert main(["migrate", *common]) == 0
    assert "applied: 1" in capsys.readouterr().out
    assert main(["status", *common]) == 0
    out = capsys.readouterr().out
    assert "current version: 1" in out and "pending: none" in out


def test_purge_reports_counts(test_dsn: str, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--connection-string", test_dsn, "--schema", schema]
    assert main(["migrate", *common]) == 0
    assert main(["purge", "--application-id", "tests", "--ttl", "60", *common]) == 0
    assert "af_documents: 0" in capsys.readouterr().out


@pytest.mark.timeout(30)
def test_wrong_password_is_a_one_line_error(capsys: pytest.CaptureFixture[str], test_dsn: str) -> None:
    started = time.monotonic()
    code = main(["status", "--connection-string", "postgresql://postgres:SECRETPW@127.0.0.1:5433/agent_framework"])
    captured = capsys.readouterr()
    assert code == 1
    assert time.monotonic() - started < 20
    assert "SECRETPW" not in captured.out + captured.err
    assert captured.err.startswith("error:")
