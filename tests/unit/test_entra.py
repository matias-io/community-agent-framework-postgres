import base64
import json
from typing import Any

import pytest
from agent_framework import SecretString
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import (
    PostgresAGUIThreadSnapshotStore,
    PostgresCheckpointStorage,
    PostgresDocumentStore,
    PostgresHistoryProvider,
    PostgresLeases,
    PostgresPersistence,
    PostgresSessionStore,
    PostgresStorageError,
)
from agent_framework_community_postgres._client import ClientHandle, create_client
from agent_framework_community_postgres._entra import ENTRA_SCOPE, entra_connection_class

TOKEN_SECRET = "TOKENSECRET"


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.{TOKEN_SECRET}"


class _Token:
    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_on = 4_102_444_800


class SyncCredential:
    def __init__(self, token: str = TOKEN_SECRET) -> None:
        self.value = token
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
        self.scopes.append(scopes)
        return _Token(self.value)


class AsyncCredential(SyncCredential):
    async def get_token(self, *scopes: str, **kwargs: Any) -> _Token:  # type: ignore[override]
        self.scopes.append(scopes)
        return _Token(self.value)


class FailingCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
        raise RuntimeError("no identity available")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_connect(cls: type[AsyncConnection[Any]], conninfo: str = "", **kwargs: Any) -> str:
        calls.append((conninfo, kwargs))
        return "connected"

    monkeypatch.setattr(AsyncConnection, "connect", classmethod(fake_connect))
    return calls


@pytest.mark.parametrize("credential_type", [SyncCredential, AsyncCredential])
async def test_the_token_is_the_password(
    captured: list[tuple[str, dict[str, Any]]], credential_type: type[SyncCredential]
) -> None:
    credential = credential_type()
    connection_class = entra_connection_class(credential)
    assert await connection_class.connect("host=db user=app", autocommit=True, connect_timeout=10) == "connected"
    conninfo, kwargs = captured[0]
    assert conninfo == "host=db user=app"
    assert kwargs["password"] == TOKEN_SECRET
    assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 10
    assert "user" not in kwargs
    assert credential.scopes == [(ENTRA_SCOPE,)]


async def test_each_new_connection_asks_for_a_token(captured: list[tuple[str, dict[str, Any]]]) -> None:
    credential = AsyncCredential()
    connection_class = entra_connection_class(credential)
    await connection_class.connect("host=db user=app")
    await connection_class.connect("host=db user=app")
    assert len(credential.scopes) == 2


@pytest.mark.parametrize(
    ("claims", "user"),
    [
        (
            {"upn": "ada@contoso.com", "preferred_username": "p@contoso.com", "unique_name": "u@contoso.com"},
            "ada@contoso.com",
        ),
        ({"preferred_username": "p@contoso.com", "unique_name": "u@contoso.com"}, "p@contoso.com"),
        ({"unique_name": "u@contoso.com"}, "u@contoso.com"),
    ],
)
async def test_user_comes_from_the_token_when_absent(
    captured: list[tuple[str, dict[str, Any]]], claims: dict[str, Any], user: str
) -> None:
    await entra_connection_class(SyncCredential(_jwt(claims))).connect("host=db dbname=app")
    assert captured[0][1]["user"] == user


async def test_a_user_in_the_connection_string_is_kept(captured: list[tuple[str, dict[str, Any]]]) -> None:
    await entra_connection_class(SyncCredential(_jwt({"upn": "ada@contoso.com"}))).connect(
        "postgresql://my-identity@db/app"
    )
    assert "user" not in captured[0][1]


@pytest.mark.parametrize("token", [_jwt({"oid": "00000000-0000-0000-0000-000000000000"}), TOKEN_SECRET, "a.!!!.b"])
async def test_no_user_anywhere_names_the_fix(captured: list[tuple[str, dict[str, Any]]], token: str) -> None:
    with pytest.raises(PostgresStorageError) as info:
        await entra_connection_class(SyncCredential(token)).connect("host=db")
    assert "user=" in str(info.value)
    assert TOKEN_SECRET not in str(info.value)
    assert captured == []


async def test_sslmode_require_is_added_only_when_missing(captured: list[tuple[str, dict[str, Any]]]) -> None:
    connection_class = entra_connection_class(SyncCredential())
    await connection_class.connect("host=db user=app")
    await connection_class.connect("host=db user=app sslmode=verify-full")
    await connection_class.connect("postgresql://app@db/app?sslmode=disable")
    assert captured[0][1]["sslmode"] == "require"
    assert "sslmode" not in captured[1][1]
    assert "sslmode" not in captured[2][1]


async def test_a_failing_credential_names_the_scope_not_a_token(captured: list[tuple[str, dict[str, Any]]]) -> None:
    with pytest.raises(PostgresStorageError) as info:
        await entra_connection_class(FailingCredential()).connect("host=db user=app")
    assert ENTRA_SCOPE in str(info.value)
    assert "no identity available" not in str(info.value)  # the credential's text stays in the chained cause
    assert isinstance(info.value.__cause__, RuntimeError)
    assert captured == []


async def test_an_empty_token_is_refused(captured: list[tuple[str, dict[str, Any]]]) -> None:
    with pytest.raises(PostgresStorageError, match="no access token"):
        await entra_connection_class(SyncCredential("")).connect("host=db user=app")


def test_a_credential_needs_get_token() -> None:
    with pytest.raises(TypeError, match="get_token"):
        entra_connection_class(object())  # type: ignore[arg-type]


async def test_the_owned_pool_uses_the_entra_connection_class() -> None:
    credential = SyncCredential()
    handle = ClientHandle(SecretString("host=127.0.0.1 dbname=app user=app"), None, credential=credential)
    try:
        assert isinstance(handle.client, AsyncConnectionPool)
        assert issubclass(handle.client.connection_class, AsyncConnection)
        assert handle.client.connection_class is not AsyncConnection
        assert TOKEN_SECRET not in repr(handle)
        assert TOKEN_SECRET not in repr(handle.client)
        assert credential.scopes == []  # nothing is fetched until a connection opens
    finally:
        await handle.close()


@pytest.mark.parametrize(
    ("store_type", "extra"),
    [
        (PostgresHistoryProvider, {}),
        (PostgresSessionStore, {}),
        (PostgresCheckpointStorage, {}),
        (PostgresAGUIThreadSnapshotStore, {}),
        (PostgresDocumentStore, {"collection": "c"}),
        (PostgresLeases, {}),
    ],
)
async def test_every_store_takes_a_credential(store_type: type[Any], extra: dict[str, Any]) -> None:
    store = store_type(
        application_id="tests", connection_string="host=127.0.0.1 user=app", credential=SyncCredential(), **extra
    )
    try:
        pool = store._client.client
        assert isinstance(pool, AsyncConnectionPool)
        assert pool.connection_class is not AsyncConnection
    finally:
        await store.close()


async def test_the_hub_shares_its_signed_in_pool() -> None:
    hub = PostgresPersistence(
        application_id="tests", connection_string="host=127.0.0.1 user=app", credential=SyncCredential()
    )
    try:
        assert isinstance(hub.pool, AsyncConnectionPool)
        assert hub.pool.connection_class is not AsyncConnection
        assert hub.history_provider()._client.client is hub.pool  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(TypeError, match="credential is set by the hub"):
            hub.session_store(credential=SyncCredential())
    finally:
        await hub.close()


async def test_a_credential_cannot_sign_in_a_borrowed_client() -> None:
    pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool("host=127.0.0.1", open=False)
    with pytest.raises(ValueError, match="credential"):
        create_client(None, client=pool, credential=SyncCredential(), env_file_path=None, env_file_encoding=None)
    with pytest.raises(ValueError, match="credential"):
        ClientHandle(None, pool, credential=SyncCredential())
    with pytest.raises(ValueError, match="credential"):
        PostgresHistoryProvider(application_id="tests", client=pool, credential=SyncCredential())
