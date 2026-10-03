import importlib
import uuid
from datetime import UTC, datetime
from importlib.metadata import requires

import agent_framework._sessions as sessions_module
import pytest
from agent_framework import Message

from agent_framework_community_postgres import _framework


def test_private_names_are_present() -> None:
    assert callable(_framework.filter_new_messages)
    assert callable(_framework.encode_checkpoint_value)
    assert callable(_framework.decode_checkpoint_value)
    assert _framework.SUPPORTED_CORE == ">=1.19.0,<1.21"


def test_supported_core_matches_the_declared_dependency() -> None:
    declared = [r for r in requires("community-agent-framework-postgres") or [] if r.startswith("agent-framework-core")]
    assert len(declared) == 1
    specifiers = declared[0].removeprefix("agent-framework-core").replace(" ", "").split(",")
    assert sorted(specifiers) == sorted(_framework.SUPPORTED_CORE.split(","))


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
        assert ">=1.19.0,<1.21" in str(info.value)
    finally:
        monkeypatch.undo()
        importlib.reload(_framework)
