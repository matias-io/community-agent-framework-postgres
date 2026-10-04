import base64
import inspect
import json
import logging
import threading
import time
from importlib.metadata import requires
from typing import Any

import pytest
from agent_framework import SecretString
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
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


async def test_a_failing_credential_is_named_with_its_cause(captured: list[tuple[str, dict[str, Any]]]) -> None:
    with pytest.raises(PostgresStorageError) as info:
        await entra_connection_class(FailingCredential()).connect("host=db user=app")
    assert ENTRA_SCOPE in str(info.value)
    assert "RuntimeError: see the chained cause" in str(info.value)
    assert "no identity available" not in str(info.value)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert captured == []


@pytest.mark.parametrize("error_type", [ClientAuthenticationError, CredentialUnavailableError])
async def test_azure_sdk_error_text_is_kept(
    captured: list[tuple[str, dict[str, Any]]], caplog: pytest.LogCaptureFixture, error_type: type[Exception]
) -> None:
    class AzureCredential:
        async def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
            raise error_type("az login required")

    with pytest.raises(PostgresStorageError) as info:
        await entra_connection_class(AzureCredential()).connect("host=db user=app")
    assert "az login required" in str(info.value)
    assert "az login required" in caplog.text
    assert isinstance(info.value.__cause__, error_type)
    assert captured == []


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_custom_credential_error_text_stays_out_of_messages_and_logs(
    captured: list[tuple[str, dict[str, Any]]], caplog: pytest.LogCaptureFixture, asynchronous: bool
) -> None:
    made_up_secret = "AUDIT_MADE_UP_SECRET_NOT_A_REAL_TOKEN"

    class CustomCredential:
        def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
            raise RuntimeError(made_up_secret)

    class AsyncCustomCredential:
        async def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
            raise RuntimeError(made_up_secret)

    connection_class = entra_connection_class(AsyncCustomCredential() if asynchronous else CustomCredential())
    with pytest.raises(PostgresStorageError) as info:
        await connection_class.connect("host=db user=app")
    assert "RuntimeError: see the chained cause" in str(info.value)
    assert made_up_secret not in str(info.value)
    assert made_up_secret not in caplog.text
    assert made_up_secret not in (connection_class.entra_last_error or "")
    assert info.value.__cause__ is not None and made_up_secret in str(info.value.__cause__)
    pool = AsyncConnectionPool("host=db user=app", open=False, connection_class=connection_class)
    try:
        assert made_up_secret not in str(ClientHandle._no_connection(pool))
    finally:
        await pool.close()
    assert captured == []


async def test_a_sync_credential_runs_off_the_event_loop(captured: list[tuple[str, dict[str, Any]]]) -> None:
    threads: list[int] = []

    class ThreadRecordingCredential:
        def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
            threads.append(threading.get_ident())
            return _Token(TOKEN_SECRET)

    await entra_connection_class(ThreadRecordingCredential()).connect("host=db user=app")
    assert threads and threads[0] != threading.get_ident()
    assert captured[0][1]["password"] == TOKEN_SECRET


async def test_pgsslmode_in_the_environment_is_respected(
    captured: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    await entra_connection_class(SyncCredential()).connect("host=db user=app")
    assert "sslmode" not in captured[0][1]  # libpq applies PGSSLMODE itself


async def test_pguser_in_the_environment_is_used_before_the_token(
    captured: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGUSER", "my-app-identity")
    await entra_connection_class(SyncCredential(_jwt({"upn": "ada@contoso.com"}))).connect("host=db")
    await entra_connection_class(SyncCredential(_jwt({"oid": "0"}))).connect("host=db")
    assert "user" not in captured[0][1]  # libpq applies PGUSER itself
    assert "user" not in captured[1][1]


def test_connect_takes_the_same_keywords_as_psycopg() -> None:
    def parameters(function: Any) -> list[tuple[str, Any, Any]]:
        return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]

    assert parameters(entra_connection_class(SyncCredential()).connect) == parameters(AsyncConnection.connect)


def test_the_azure_extra_installs_an_async_transport() -> None:
    declared = requires("community-agent-framework-postgres") or []
    azure = [r.split(";")[0] for r in declared if "extra == 'azure'" in r.replace('"', "'")]
    assert any(r.startswith("aiohttp") for r in azure), azure
    from azure.core.pipeline.transport import AioHttpTransport

    assert AioHttpTransport is not None


async def test_open_fetches_a_token_before_the_pool_opens() -> None:
    handle = ClientHandle(SecretString("host=127.0.0.1 dbname=app user=app"), None, credential=FailingCredential())
    try:
        with pytest.raises(PostgresStorageError) as info:
            await handle.open()
        assert "RuntimeError: see the chained cause" in str(info.value)
        assert isinstance(info.value.__cause__, RuntimeError)
        assert isinstance(handle.client, AsyncConnectionPool) and handle.client.closed
    finally:
        await handle.close()


async def test_a_hub_store_runs_the_credential_check_on_first_use() -> None:
    hub = PostgresPersistence(
        application_id="tests", connection_string="host=127.0.0.1 user=app", credential=FailingCredential()
    )
    try:
        started = time.monotonic()
        with pytest.raises(PostgresStorageError, match="RuntimeError: see the chained cause"):
            await hub.history_provider().get_messages("s")
        assert time.monotonic() - started < 5
    finally:
        await hub.close()


async def test_a_sign_in_failure_inside_the_pool_is_logged_and_named_by_the_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExpiringCredential:
        """Signs in once, for the eager check, then fails like an expired Azure CLI login."""

        def __init__(self) -> None:
            self.calls = 0

        async def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
            self.calls += 1
            if self.calls > 1:
                raise ClientAuthenticationError("az login required")
            return _Token(TOKEN_SECRET)

    handle = ClientHandle(SecretString("host=127.0.0.1 dbname=app user=app"), None, credential=ExpiringCredential())
    assert isinstance(handle.client, AsyncConnectionPool)
    handle.client.timeout = 1.0
    caplog.set_level(logging.ERROR, logger="agent_framework_community_postgres")
    try:
        with pytest.raises(PostgresStorageError) as info:
            async with handle.connection():
                pass
        message = str(info.value)
        assert "No PostgreSQL connection became available within 1 second" in message
        assert "Last connection error:" in message and "ClientAuthenticationError: az login required" in message
        logged = [r for r in caplog.records if r.name.startswith("agent_framework_community_postgres")]
        assert logged and "ClientAuthenticationError: az login required" in logged[0].getMessage()
        assert all(r.levelno == logging.ERROR for r in logged)
        assert TOKEN_SECRET not in message and TOKEN_SECRET not in caplog.text
    finally:
        await handle.close()


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
