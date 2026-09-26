import pytest

from agent_framework_community_postgres._cli import main


def test_migrate_print_renders_sql_without_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--print", "--schema", "agents"]) == 0
    out = capsys.readouterr().out
    assert '"agents"."af_documents"' in out
    assert "-- version 1" in out


def test_purge_requires_application_id_and_ttl(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--connection-string", "host=x"])
