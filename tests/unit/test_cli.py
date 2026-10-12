import pytest

from agent_framework_community_postgres._cli import main


def test_migrate_print_renders_sql_without_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--print", "--schema", "agents"]) == 0
    out = capsys.readouterr().out
    assert '"agents"."af_sessions"' in out
    assert "-- version 1" in out


def test_migrate_print_from_version_prints_only_later_versions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--print", "--from-version", "1"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["migrate", "--print", "--from-version", "0"]) == 0
    assert "-- version 1" in capsys.readouterr().out


def test_from_version_needs_print(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["migrate", "--from-version", "1"])
    assert info.value.code == 2
    assert "--from-version needs --print" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["status", "--connection-string=host=x"], ["--connection-string=host=x", "status"]])
def test_connection_string_option_is_gone(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2
    assert "--connection-string" in capsys.readouterr().err


def test_purge_requires_application_id(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["purge"])


def test_purge_has_no_ttl_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--application-id", "a", "--ttl", "5"])
    assert "--ttl" in capsys.readouterr().err


def test_missing_connection_string_is_one_line_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_CONNECTION_STRING", raising=False)
    assert main(["status"]) == 1
    assert capsys.readouterr().err == "error: set POSTGRES_CONNECTION_STRING\n"


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
        ["status", "--schema", "Bad"],
        ["migrate", "--print", "--table-prefix", "p" * 40],
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
