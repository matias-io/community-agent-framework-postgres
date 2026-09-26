import pytest
from agent_framework import SecretString
from agent_framework.exceptions import SettingNotFoundError
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres._client import (
    ClientHandle,
    PostgresStorageError,
    TableNames,
    create_client,
    optional_text,
    require_text,
)


def test_table_names_render_schema_qualified_identifiers() -> None:
    names = TableNames(schema="agents", prefix="af_")
    assert names.table("documents").as_string() == '"agents"."af_documents"'
    assert names.index("documents_scope_idx").as_string() == '"af_documents_scope_idx"'
    assert names.qualified("documents") == "agents.af_documents"


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
