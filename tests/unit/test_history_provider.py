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


@pytest.mark.parametrize("kwargs", [{"tenant_id": ""}, {"agent_id": ""}, {"max_messages": -1}])
def test_rejects_empty_scopes_and_negative_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PostgresHistoryProvider(application_id="app", client=_pool(), **kwargs)  # type: ignore[arg-type]


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
        await provider.get_messages(5)  # type: ignore[arg-type]
