from typing import Any, cast

import pytest
from agent_framework import Message
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres._history_provider import PostgresHistoryProvider


def _pool() -> AsyncConnectionPool:
    return AsyncConnectionPool("host=x", open=False)


def test_defaults_match_the_redis_provider_shape() -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool())
    assert provider.source_id == "postgres_history"
    assert provider.load_messages and provider.store_inputs and provider.store_outputs
    assert provider.tenant_id == "" and provider.agent_id == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tenant_id": ""},
        {"agent_id": ""},
        {"max_messages": -1},
        {"max_messages": float("inf")},
        {"max_messages": float("nan")},
        {"max_messages": 2.5},
    ],
)
def test_rejects_empty_scopes_and_negative_limits(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PostgresHistoryProvider(application_id="app", client=_pool(), **kwargs)


@pytest.mark.parametrize(("given", "stored"), [(None, None), (0, 0), (3, 3), (2.0, 2), (False, 0), (True, 1)])
def test_accepts_the_limits_0_1_1_accepted(given: Any, stored: int | None) -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool(), max_messages=given)
    assert provider.max_messages == stored and (stored is None or type(provider.max_messages) is int)


async def test_none_session_id_is_an_error() -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool())
    with pytest.raises(ValueError):
        await provider.get_messages(None)


@pytest.mark.parametrize("session_id", ["", None])
async def test_empty_or_missing_session_id_is_rejected_everywhere(session_id: str | None) -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool())
    with pytest.raises(ValueError):
        await provider.get_messages(session_id)
    with pytest.raises(ValueError):
        await provider.save_messages(session_id, [Message(role="user", contents=["a"])])
    with pytest.raises(ValueError):
        await provider.clear(session_id)


async def test_non_string_session_id_is_rejected() -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool())
    with pytest.raises(ValueError):
        await provider.get_messages(cast(Any, 5))


async def test_false_message_limit_still_disables_writes_without_a_connection() -> None:
    provider = PostgresHistoryProvider(application_id="app", client=_pool(), max_messages=False)
    await provider.save_messages("session", [Message(role="user", contents=["not stored"])])
