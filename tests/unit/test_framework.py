import importlib
import subprocess
import sys
import uuid
import warnings
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, requires

import agent_framework._sessions as sessions_module
import pytest
from agent_framework import Message

from agent_framework_community_postgres import UntestedAgentFrameworkWarning, _framework


def test_private_names_are_present() -> None:
    assert callable(_framework.filter_new_messages)
    assert callable(_framework.encode_checkpoint_value)
    assert callable(_framework.decode_checkpoint_value)
    assert _framework.SUPPORTED_CORE == ">=1.19.0,<2"


def _declared(distribution: str) -> list[str]:
    found = [r for r in requires("community-agent-framework-postgres") or [] if r.startswith(distribution)]
    assert len(found) == 1, found
    return sorted(found[0].split(";")[0].removeprefix(distribution).replace(" ", "").split(","))


def test_supported_core_matches_the_declared_dependency() -> None:
    assert _declared("agent-framework-core") == sorted(_framework.SUPPORTED_CORE.split(","))
    assert _declared("agent-framework-ag-ui") == sorted([">=1.4.0", "<2"])


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


def _untested_warnings() -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _framework.warn_if_untested_agent_framework()
    assert all(w.category is UntestedAgentFrameworkWarning for w in caught)
    return [str(w.message) for w in caught]


def test_a_newer_core_minor_warns_with_the_versions_and_the_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.21.0")
    [message] = _untested_warnings()
    assert message.startswith("community-agent-framework-postgres has not been tested with agent-framework-core 1.21.0")
    assert "(tested: 1.19, 1.20)" in message
    assert "agent-framework-core<1.21" in message
    assert "filterwarnings" in message


@pytest.mark.parametrize("installed", ["1.20.3", "1.20.0", "1.19.0", "1.18.2", "1.20.5rc1", "not-a-version"])
def test_tested_older_and_unparseable_core_versions_do_not_warn(
    monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    _installed(monkeypatch, agent_framework_core=installed, agent_framework_ag_ui="1.5.0")
    assert _untested_warnings() == []


def test_a_pre_release_of_a_newer_minor_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.21.0b1")
    [message] = _untested_warnings()
    assert "agent-framework-core 1.21.0b1" in message


def test_a_newer_ag_ui_minor_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.20.0", agent_framework_ag_ui="1.6.0")
    [message] = _untested_warnings()
    assert "agent-framework-ag-ui 1.6.0 (tested: 1.4, 1.5)" in message
    assert "agent-framework-ag-ui<1.6" in message


def test_without_ag_ui_only_core_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.20.0")
    assert _untested_warnings() == []


def test_the_warning_can_be_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="2.0.0", agent_framework_ag_ui="1.9.0")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=UntestedAgentFrameworkWarning)
        _framework.warn_if_untested_agent_framework()
    assert caught == []


def test_the_message_filter_silences_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, agent_framework_core="1.21.0")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", message="community-agent-framework-postgres has not been tested")
        _framework.warn_if_untested_agent_framework()
    assert caught == []


def test_importing_the_package_warns_at_the_import() -> None:
    script = (
        "import importlib.metadata as metadata, warnings\n"
        "real = metadata.version\n"
        "metadata.version = lambda d: '1.21.0' if d == 'agent-framework-core' else real(d)\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always')\n"
        "    import agent_framework_community_postgres  # the warning should point here\n"
        "found = [w for w in caught if w.category.__name__ == 'UntestedAgentFrameworkWarning']\n"
        "print(len(found), found[0].filename if found else '')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60)
    count, filename = result.stdout.split()
    assert count == "1"
    assert filename == "<string>"


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
        assert ">=1.19.0,<2" in str(info.value) and "1.19, 1.20" in str(info.value)
    finally:
        monkeypatch.undo()
        importlib.reload(_framework)
