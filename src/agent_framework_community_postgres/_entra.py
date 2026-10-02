"""Microsoft Entra ID sign-in for Azure Database for PostgreSQL: a fresh token is the password of each new connection.

azure-identity stays optional: a credential is anything with ``get_token(*scopes)``, sync or async, so this
module never imports ``azure``. PostgreSQL checks the password only at login, so a connection stays
authenticated after its token expires; the pool's ``max_lifetime`` bounds how long such a connection lives.
"""

from __future__ import annotations

import base64
import inspect
import json
from collections.abc import Awaitable
from typing import Any, Protocol, Self, cast

from psycopg import AsyncConnection, AsyncCursor
from psycopg.abc import AdaptContext, ConnParam
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import AsyncRowFactory

from ._client import PostgresStorageError

ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
"""The scope Azure Database for PostgreSQL accepts tokens for."""

# Claims that carry a user principal name, in the order a token is searched for one.
_USER_CLAIMS = ("upn", "preferred_username", "unique_name")


class EntraAccessToken(Protocol):
    """What ``get_token`` returns; ``azure.core.credentials.AccessToken`` fits."""

    @property
    def token(self) -> str: ...

    @property
    def expires_on(self) -> int: ...


class EntraCredential(Protocol):
    """A Microsoft Entra ID credential from ``azure.identity`` or ``azure.identity.aio``, or your own."""

    def get_token(self, *scopes: str, **kwargs: Any) -> EntraAccessToken | Awaitable[EntraAccessToken]: ...


async def _fetch_token(credential: EntraCredential) -> str:
    try:
        result = credential.get_token(ENTRA_SCOPE)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        # The token was never issued, so the chained credential error cannot contain it.
        raise PostgresStorageError(
            f"Could not get a Microsoft Entra ID token for {ENTRA_SCOPE}; see the chained credential error."
        ) from exc
    token: object = getattr(result, "token", None)
    if not isinstance(token, str) or not token:
        raise PostgresStorageError(f"The credential returned no access token for {ENTRA_SCOPE}.")
    return token


def _user_from_token(token: str) -> str | None:
    """The user principal name in the token's unverified payload, or ``None``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims: object = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError are all ValueError
        return None
    if not isinstance(claims, dict):
        return None
    for claim in _USER_CLAIMS:
        value = cast("dict[str, object]", claims).get(claim)
        if isinstance(value, str) and value:
            return value
    return None


async def entra_connection_kwargs(credential: EntraCredential, conninfo: str, kwargs: dict[str, ConnParam]) -> None:
    """Set ``password`` to a fresh token, ``user`` from the token when absent, and ``sslmode=require`` when unset."""
    params = conninfo_to_dict(conninfo)
    token = await _fetch_token(credential)
    kwargs["password"] = token
    if not params.get("user") and not kwargs.get("user"):
        user = _user_from_token(token)
        if user is None:
            raise PostgresStorageError(
                "The Microsoft Entra ID token names no user (upn, preferred_username or unique_name);"
                " set user= in the connection string to the database role of this identity."
            )
        kwargs["user"] = user
    if "sslmode" not in params and "sslmode" not in kwargs:
        kwargs["sslmode"] = "require"


def entra_connection_class(credential: EntraCredential) -> type[AsyncConnection[Any]]:
    """An ``AsyncConnection`` subclass whose ``connect`` signs in with a token from ``credential``."""
    if not callable(getattr(credential, "get_token", None)):
        raise TypeError("credential must have a get_token(*scopes) method, like the azure.identity credentials.")

    class EntraConnection(AsyncConnection[Any]):
        @classmethod
        async def connect(
            cls,
            conninfo: str = "",
            *,
            autocommit: bool = False,
            prepare_threshold: int | None = 5,
            context: AdaptContext | None = None,
            row_factory: AsyncRowFactory[Any] | None = None,
            cursor_factory: type[AsyncCursor[Any]] | None = None,
            **kwargs: ConnParam,
        ) -> Self:
            await entra_connection_kwargs(credential, conninfo, kwargs)
            return await super().connect(
                conninfo,
                autocommit=autocommit,
                prepare_threshold=prepare_threshold,
                context=context,
                row_factory=row_factory,
                cursor_factory=cursor_factory,
                **kwargs,
            )

    return EntraConnection
