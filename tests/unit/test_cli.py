import pytest

from agent_framework_community_postgres._cli import main


def test_migrate_print_renders_sql_without_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--print", "--schema", "agents"]) == 0
    out = capsys.readouterr().out
    assert '"agents"."af_documents"' in out
    assert "-- version 1" in out


def test_purge_requires_application_id(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--connection-string", "host=x"])


def test_purge_has_no_ttl_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--application-id", "a", "--ttl", "5", "--connection-string", "host=x"])
    assert "--ttl" in capsys.readouterr().err


def test_missing_connection_string_is_one_line_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_CONNECTION_STRING", raising=False)
    assert main(["status"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_purge_with_application_id_reaches_the_connection_check(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_CONNECTION_STRING", raising=False)
    assert main(["purge", "--application-id", "a"]) == 1
    assert "POSTGRES_CONNECTION_STRING" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate", "--print", "--schema", "Bad"],
        ["status", "--schema", "Bad", "--connection-string", "host=127.0.0.1 port=1"],
        ["migrate", "--print", "--table-prefix", "p" * 40],
        ["status", "--connection-string", ""],
    ],
)
def test_invalid_options_are_one_line_errors_with_exit_code_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
