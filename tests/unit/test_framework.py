import uuid
from datetime import UTC, datetime

from agent_framework import Message

from agent_framework_community_postgres import _framework


def test_private_names_are_present() -> None:
    assert callable(_framework.filter_new_messages)
    assert callable(_framework.encode_checkpoint_value)
    assert callable(_framework.decode_checkpoint_value)
    assert _framework.SUPPORTED_CORE == ">=1.19.0,<2"


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
