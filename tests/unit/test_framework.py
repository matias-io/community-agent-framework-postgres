import importlib
import logging
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, requires

import agent_framework._sessions as sessions_module
import pytest
from agent_framework import Message

from agent_framework_community_postgres import _framework


def test_private_names_are_present() -> None:
    assert callable(_framework.filter_new_messages)
    assert callable(_framework.encode_checkpoint_value)
    assert callable(_framework.decode_checkpoint_value)
    assert _framework.SUPPORTED_CORE == ">=1.20.0,<2"


def _declared(distribution: str) -> list[str]:
    found = [r for r in requires("community-agent-framework-postgres") or [] if r.startswith(distribution)]
    assert len(found) == 1, found
    return sorted(found[0].split(";")[0].removeprefix(distribution).replace(" ", "").split(","))


def test_supported_core_matches_the_declared_dependency() -> None:
    assert _declared("agent-framework-core") == sorted(_framework.SUPPORTED_CORE.split(","))
    assert _declared("agent-framework-ag-ui") == sorted([">=1.5.0", "<2"])


def test_the_lowest_tested_minor_is_the_declared_floor() -> None:
    assert _framework.SUPPORTED_CORE.startswith(f">={_framework.TESTED_CORE[0]}.0,")
    assert _declared("agent-framework-ag-ui")[1] == f">={_framework.TESTED_AG_UI[0]}.0"


def _installed(monkeypatch: pytest.MonkeyPatch, **versions: str) -> None:
    def fake_version(distribution: str) -> str:
        key = distribution.replace("-", "_")
        if key not in versions:
            raise PackageNotFoundError(distribution)
        return versions[key]

    monkeypatch.setattr(_framework, "version", fake_version)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "agent_framework_community_postgres"]


def test_compatibility_reports_installed_and_tested(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.20.0")
    report = _framework.compatibility()
    assert report.core_installed == "1.20.0"
    assert report.core_tested == _framework.TESTED_CORE
    assert report.ag_ui_installed is None
    assert report.untested == () and report.out_of_range == ()


def test_a_newer_minor_is_untested(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.22.0", agent_framework_ag_ui="1.6.0")
    report = _framework.compatibility()
    assert report.untested == ("agent-framework-core", "agent-framework-ag-ui")
    assert report.out_of_range == ()


def test_a_newer_major_is_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="2.0.0")
    report = _framework.compatibility()
    assert report.untested == ("agent-framework-core",)
    assert report.out_of_range == ("agent-framework-core",)


@pytest.mark.parametrize("installed", ["1.21.0", "1.20.3", "1.20.0", "1.19.0", "1.20.5rc1", "not-a-version"])
def test_tested_older_and_unparseable_versions_are_not_untested(
    monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    _installed(monkeypatch, agent_framework_core=installed, agent_framework_ag_ui="1.5.0")
    assert _framework.compatibility().untested == ()


def test_log_untested_once_logs_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _installed(monkeypatch, agent_framework_core="1.22.0")
    monkeypatch.setattr(_framework, "_logged", False)
    with caplog.at_level(logging.WARNING, logger="agent_framework_community_postgres"):
        _framework.log_untested_once()
        _framework.log_untested_once()
    [message] = _messages(caplog)
    assert message.startswith("community-agent-framework-postgres has not been tested with agent-framework-core 1.22.0")
    assert (
        "(tested: 1.20, 1.21)" in message and "expected to work" in message and "agent-framework-core<1.22" in message
    )


def test_a_newer_major_is_logged_as_likely_to_break(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _installed(monkeypatch, agent_framework_core="2.0.0")
    monkeypatch.setattr(_framework, "_logged", False)
    with caplog.at_level(logging.WARNING, logger="agent_framework_community_postgres"):
        _framework.log_untested_once()
    [message] = _messages(caplog)
    assert "outside the declared range and likely to break" in message and "agent-framework-core<2 " in message
    assert "expected to work" not in message


def test_tested_versions_log_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _installed(monkeypatch, agent_framework_core="1.20.0")
    monkeypatch.setattr(_framework, "_logged", False)
    with caplog.at_level(logging.WARNING, logger="agent_framework_community_postgres"):
        _framework.log_untested_once()
    assert _messages(caplog) == []


def test_importing_the_package_has_no_side_effect() -> None:
    script = (
        "import importlib.metadata as metadata, warnings, logging\n"
        "real = metadata.version\n"
        "metadata.version = lambda d: '1.22.0' if d == 'agent-framework-core' else real(d)\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('error')\n"
        "    import agent_framework_community_postgres\n"
        "print(len(caught))\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", script], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == "0"
    assert "has not been tested" not in result.stderr


def test_the_first_store_logs_the_check(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from agent_framework_community_postgres import PostgresSessionStore

    _installed(monkeypatch, agent_framework_core="1.22.0")
    monkeypatch.setattr(_framework, "_logged", False)
    with caplog.at_level(logging.WARNING, logger="agent_framework_community_postgres"):
        PostgresSessionStore(application_id="a", connection_string="postgresql://u:p@127.0.0.1:1/db")
    assert len(_messages(caplog)) == 1


def test_checkpoint_encoding_round_trips_non_json_values() -> None:
    value = {"when": datetime(2026, 9, 30, tzinfo=UTC), "id": uuid.uuid4(), "n": 1, "nested": {"list": [1, "a"]}}
    encoded = _framework.encode_checkpoint_value(value)
    assert isinstance(encoded, dict)
    assert _framework.decode_checkpoint_value(encoded, allowed_types=frozenset()) == value


def test_filter_new_messages_drops_a_replayed_prefix() -> None:
    first = Message(role="user", contents=["hello"])
    second = Message(role="assistant", contents=["hi"])
    third = Message(role="user", contents=["more"])
    new = _framework.filter_new_messages([first, second], [first, second, third])
    assert [m.text for m in new] == ["more"]


def test_a_missing_private_name_is_named_in_the_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sessions_module, "filter_new_messages")
    try:
        with pytest.raises(ImportError) as info:
            importlib.reload(_framework)
        assert "filter_new_messages" in str(info.value)
        assert ">=1.20.0,<2" in str(info.value) and "tested on 1.20, 1.21;" in str(info.value)
    finally:
        monkeypatch.undo()
        importlib.reload(_framework)
