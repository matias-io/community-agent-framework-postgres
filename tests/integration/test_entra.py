from typing import Any

import pytest
from agent_framework import Message
from psycopg import OperationalError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._entra import ENTRA_SCOPE, entra_connection_class

pytestmark = pytest.mark.integration


class _Token:
    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_on = 4_102_444_800


class PasswordAsToken:
    """A stand-in credential whose token is the test database's password."""

    def __init__(self, token: str) -> None:
        self.value = token
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str, **kwargs: Any) -> _Token:
        self.scopes.append(scopes)
        return _Token(self.value)


def _split(test_dsn: str) -> tuple[str, str]:
    """The test DSN without its password, with sslmode=disable (the local container has no TLS), and the password."""
    options = conninfo_to_dict(test_dsn)
    password = str(options.pop("password", "") or "")
    if not password:
        pytest.skip("POSTGRES_TEST_CONNECTION_STRING has no password to hand out as a token.")
    options["sslmode"] = "disable"
    return make_conninfo("", **options), password


async def test_the_token_reaches_postgres_as_the_password(test_dsn: str, schema: str) -> None:
    conninfo, password = _split(test_dsn)
    credential = PasswordAsToken(password)
    async with PostgresPersistence(
        application_id="tests", connection_string=conninfo, schema=schema, credential=credential
    ) as hub:
        await hub.migrate()
        history = hub.history_provider()
        await history.save_messages("s", [Message(role="user", contents=["signed in with a token"])])
        assert [m.text for m in await history.get_messages("s")] == ["signed in with a token"]
    assert credential.scopes and set(credential.scopes) == {(ENTRA_SCOPE,)}


async def test_a_wrong_token_is_refused_by_postgres(test_dsn: str) -> None:
    conninfo, _ = _split(test_dsn)
    with pytest.raises(OperationalError) as info:
        await entra_connection_class(PasswordAsToken("not-the-password")).connect(conninfo)
    assert "not-the-password" not in str(info.value)
