from typing import Any

import pytest
from agent_framework import SecretString
from agent_framework.exceptions import SettingNotFoundError
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres._client import (
    ClientHandle,
    PostgresStorageError,
    TableNames,
    create_client,
    is_retryable,
    optional_text,
    require_text,
)


def test_table_names_render_schema_qualified_identifiers() -> None:
    names = TableNames(schema="agents", prefix="af_")
    assert names.table("sessions").as_string() == '"agents"."af_sessions"'
    assert names.index("history_messages_session_idx").as_string() == '"af_history_messages_session_idx"'
    assert names.qualified("sessions") == "agents.af_sessions"


@pytest.mark.parametrize("schema", ["Public", "my schema", 'x"y', "", "1abc", "public\n", "pg_custom"])
def test_table_names_reject_unsafe_schema(schema: str) -> None:
    with pytest.raises(ValueError):
        TableNames(schema=schema)


def test_table_names_reject_prefix_with_trailing_newline() -> None:
    with pytest.raises(ValueError):
        TableNames(prefix="af_\n")


def test_table_names_reject_prefix_that_overflows_identifier_limit() -> None:
    with pytest.raises(ValueError):
        TableNames(prefix="p" * 50)


def test_table_names_bound_the_longest_derived_index_name() -> None:
    names = TableNames(prefix="p" * 35)
    assert names.index("history_messages_session_idx").as_string() == f'"{"p" * 35}history_messages_session_idx"'
    with pytest.raises(ValueError):
        TableNames(prefix="p" * 36)


def test_empty_prefix_is_allowed() -> None:
    assert TableNames(prefix="").table("sessions").as_string() == '"public"."sessions"'


def test_require_and_optional_text() -> None:
    assert require_text("abc", "x") == "abc"
    assert optional_text(None, "x") == ""
    for bad in ("", 3, None):
        with pytest.raises(ValueError):
            require_text(bad, "x")
    with pytest.raises(ValueError):
        optional_text("", "x")


def test_create_client_explicit_connection_string_beats_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_CONNECTION_STRING", "host=environment password=env-secret")
    client = create_client(
        "host=explicit password=explicit-secret", client=None, env_file_path=None, env_file_encoding=None
    )
    assert client.owned
    assert isinstance(client.client, AsyncConnectionPool)
    assert client.client.conninfo == "host=explicit password=explicit-secret"
    assert client.client.closed


def test_create_client_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_CONNECTION_STRING", "host=environment password=env-secret")
    client = create_client(None, client=None, env_file_path=None, env_file_encoding=None)
    assert client.client.conninfo == "host=environment password=env-secret"


def test_create_client_requires_exactly_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_CONNECTION_STRING", raising=False)
    with pytest.raises(SettingNotFoundError):
        create_client(None, client=None, env_file_path=None, env_file_encoding=None)
    pool = AsyncConnectionPool("host=x", open=False)
    with pytest.raises(ValueError):
        create_client("host=y", client=pool, env_file_path=None, env_file_encoding=None)


def test_secret_string_is_accepted_and_never_rendered() -> None:
    client = ClientHandle(SecretString("host=h password=hidden-value"), None)
    assert "hidden-value" not in repr(client)


def test_borrowed_client_must_be_psycopg_object() -> None:
    with pytest.raises(TypeError):
        ClientHandle(None, object())  # type: ignore[arg-type]


async def test_closed_client_refuses_connections() -> None:
    client = ClientHandle(SecretString("host=h"), None)
    await client.close()
    with pytest.raises(PostgresStorageError):
        async with client.connection():
            pass


async def test_child_handle_refuses_connections_after_parent_closes() -> None:
    parent = ClientHandle(SecretString("host=h"), None)
    child = parent.child()
    assert child.client is parent.client
    assert not child.owned
    await parent.close()
    with pytest.raises(PostgresStorageError):
        async with child.connection():
            pass
    with pytest.raises(PostgresStorageError):
        await child.open()


async def test_closing_a_child_leaves_the_parent_open() -> None:
    parent = ClientHandle(SecretString("host=h"), None)
    child = parent.child()
    await child.close()
    assert child.closed
    assert not parent.closed
    await parent.close()


def test_create_client_borrows_a_handle_as_a_child() -> None:
    parent = ClientHandle(SecretString("host=h"), None)
    child = create_client(None, client=parent, env_file_path=None, env_file_encoding=None)
    assert child is not parent
    assert child.client is parent.client
    assert not child.owned
    with pytest.raises(ValueError):
        create_client("host=h", client=parent, env_file_path=None, env_file_encoding=None)


def test_owned_pool_defaults_connect_timeout_to_ten() -> None:
    handle = ClientHandle(SecretString("host=127.0.0.1 dbname=x"), None)
    assert isinstance(handle.client, AsyncConnectionPool)
    assert handle.client.kwargs["connect_timeout"] == 10
    assert handle.client.timeout == 10.0


def test_owned_pool_keeps_a_conninfo_connect_timeout() -> None:
    handle = ClientHandle(SecretString("host=127.0.0.1 dbname=x connect_timeout=3"), None)
    assert isinstance(handle.client, AsyncConnectionPool)
    assert "connect_timeout" not in handle.client.kwargs  # the conninfo's own 3 stays in force


def test_invalid_conninfo_is_reported_without_echoing_it() -> None:
    with pytest.raises(PostgresStorageError) as info:
        ClientHandle(SecretString("garbage SECRETPW"), None)
    assert "SECRETPW" not in str(info.value)
    assert info.value.__cause__ is None


def test_retryable_sqlstates() -> None:
    assert is_retryable("08006")  # connection failure
    assert is_retryable("40001")  # serialization failure
    assert is_retryable("40P01")  # deadlock
    assert is_retryable("53300")  # too many connections
    assert is_retryable("57P01")  # admin shutdown
    assert is_retryable("55P03")  # lock not available
    assert not is_retryable("42P01")  # undefined table
    assert not is_retryable("23505")  # unique violation
    assert not is_retryable(None)


def test_storage_error_carries_classification() -> None:
    error = PostgresStorageError("x", sqlstate="40001", retryable=True)
    assert error.sqlstate == "40001" and error.retryable is True
    assert PostgresStorageError("x").retryable is False and PostgresStorageError("x").sqlstate is None


def test_owned_pool_connection_defaults() -> None:
    handle = ClientHandle(SecretString("postgresql://u:p@127.0.0.1:5433/db"), None)
    assert isinstance(handle.client, AsyncConnectionPool)
    kwargs = handle.client.kwargs
    assert kwargs["connect_timeout"] == 10
    assert kwargs["keepalives"] == 1
    assert kwargs["tcp_user_timeout"] == 30000
    assert "options" not in kwargs
    assert handle.client.max_size == 10


def test_conninfo_values_win_over_defaults() -> None:
    handle = ClientHandle(SecretString("postgresql://u:p@127.0.0.1:5433/db?connect_timeout=3&keepalives=0"), None)
    assert isinstance(handle.client, AsyncConnectionPool)
    assert "connect_timeout" not in handle.client.kwargs
    assert "keepalives" not in handle.client.kwargs


def test_pool_kwargs_override_pool_and_connection_defaults() -> None:
    handle = ClientHandle(
        SecretString("postgresql://u:p@127.0.0.1:5433/db"),
        None,
        pool_kwargs={"max_size": 50, "kwargs": {"prepare_threshold": None}},
    )
    assert isinstance(handle.client, AsyncConnectionPool)
    assert handle.client.max_size == 50
    assert handle.client.kwargs["prepare_threshold"] is None
    assert handle.client.kwargs["keepalives"] == 1


class _Credential:
    def get_token(self, *scopes: str) -> None:
        raise AssertionError("not called")


def test_pool_kwargs_cannot_replace_the_entra_connection_class() -> None:
    with pytest.raises(ValueError, match="connection_class"):
        ClientHandle(
            SecretString("postgresql://u:p@127.0.0.1:5433/db"),
            None,
            credential=_Credential(),  # type: ignore[arg-type]
            pool_kwargs={"connection_class": AsyncConnection},
        )


def test_weak_sslmode_is_refused_with_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSSLMODE", raising=False)
    for mode in ("disable", "allow", "prefer"):
        with pytest.raises(ValueError, match=f"sslmode={mode}"):
            ClientHandle(SecretString(f"postgresql://u@h/db?sslmode={mode}"), None, credential=_Credential())  # type: ignore[arg-type]
    monkeypatch.setenv("PGSSLMODE", "prefer")
    with pytest.raises(ValueError, match="PGSSLMODE"):
        ClientHandle(SecretString("postgresql://u@h/db"), None, credential=_Credential())  # type: ignore[arg-type]
    monkeypatch.delenv("PGSSLMODE")
    ClientHandle(SecretString("postgresql://u@h/db?sslmode=require"), None, credential=_Credential())  # type: ignore[arg-type]
    ClientHandle(SecretString("postgresql://u@h/db?sslmode=prefer"), None)  # no credential: the user's choice


def test_the_sslmode_libpq_would_use_is_the_one_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "prefer")
    ClientHandle(SecretString("postgresql://u@h/db?sslmode=verify-full"), None, credential=_Credential())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sslmode=disable"):
        ClientHandle(
            SecretString("postgresql://u@h/db?sslmode=verify-full"),
            None,
            credential=_Credential(),  # type: ignore[arg-type]
            pool_kwargs={"kwargs": {"sslmode": "disable"}},
        )


def test_pool_kwargs_cannot_configure_a_borrowed_client() -> None:
    pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool("host=127.0.0.1", open=False)
    with pytest.raises(ValueError, match="pool_kwargs"):
        create_client(None, client=pool, env_file_path=None, env_file_encoding=None, pool_kwargs={"max_size": 2})
    with pytest.raises(ValueError, match="pool_kwargs"):
        ClientHandle(None, pool, pool_kwargs={"max_size": 2})


async def test_stores_and_the_hub_take_pool_kwargs() -> None:
    from agent_framework_community_postgres import (
        PostgresCheckpointStorage,
        PostgresHistoryProvider,
        PostgresLeases,
        PostgresPersistence,
        PostgresSessionStore,
    )

    dsn = "host=127.0.0.1 dbname=x"
    for store_type in (PostgresHistoryProvider, PostgresSessionStore, PostgresCheckpointStorage, PostgresLeases):
        store = store_type(application_id="tests", connection_string=dsn, pool_kwargs={"max_size": 3})
        pool = store._client.client  # pyright: ignore[reportPrivateUsage]
        assert isinstance(pool, AsyncConnectionPool) and pool.max_size == 3
        await store.close()
    hub = PostgresPersistence(application_id="tests", connection_string=dsn, pool_kwargs={"max_size": 4})
    assert isinstance(hub.pool, AsyncConnectionPool) and hub.pool.max_size == 4
    with pytest.raises(TypeError, match="pool_kwargs is set by the hub"):
        hub.session_store(pool_kwargs={"max_size": 5})
    await hub.close()
