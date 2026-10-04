# Microsoft Entra ID on Azure Database for PostgreSQL

Pass `credential=` and the pool this package creates signs in to Azure Database for PostgreSQL with a Microsoft Entra ID token instead of a password. It plays the role of the `credential=` argument on Microsoft's Cosmos DB integration and follows Microsoft's documented psycopg 3 pattern: a connection class whose `connect` fetches a token and passes it as the password.

Verified with a fake credential against PostgreSQL 17 locally; the CI matrix runs the same tests on PostgreSQL 16 and 17. No real Azure server has been used yet.

## Prerequisites

- An Azure Database for PostgreSQL flexible server with Microsoft Entra authentication enabled.
- The Entra user, group, service principal or managed identity added to the server as a database role, with the privileges `migrate()` needs on the schema. See [migrations.md](migrations.md).
- The `azure` extra, which installs `azure-identity` and `aiohttp`. The `azure.identity.aio` credentials need `aiohttp` as their HTTP transport and raise `ImportError` without it:

```bash
pip install "community-agent-framework-postgres[azure]"
uv add "community-agent-framework-postgres[azure]"
```

## Connect

The connection string names the host, the database and usually the role. It holds no password.

```python
import asyncio
import sys

from azure.identity.aio import DefaultAzureCredential

from agent_framework_community_postgres import PostgresPersistence

DSN = "host=my-server.postgres.database.azure.com dbname=agent_framework user=ada@contoso.com"


async def main() -> None:
    async with (
        DefaultAzureCredential() as credential,
        PostgresPersistence(application_id="my-app", connection_string=DSN, credential=credential) as hub,
    ):
        await hub.migrate()
        print(await hub.pending_migrations())


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

On Azure compute, a managed identity is the usual choice. Its token carries no user name, and neither does a service principal's (for example from `ClientSecretCredential`). For either, set `user=` to the database role you created for it, which is normally the identity's or the application's name. `PGUSER` in the environment works too:

```python
from azure.identity.aio import ManagedIdentityCredential

credential = ManagedIdentityCredential(client_id="<client id of a user-assigned identity>")
hub = PostgresPersistence(
    application_id="my-app",
    connection_string="host=my-server.postgres.database.azure.com dbname=agent_framework user=my-app-identity",
    credential=credential,
)
```

Every store takes `credential=` too, next to `connection_string=`. The credential applies to the pool built from the connection string, so it cannot be combined with `client=`; a pool or connection you pass signs in on its own. A hub's factories raise `TypeError` if you pass them a `credential`, because their stores share the hub's pool.

## What the package does

For each new connection the pool opens, the package:

1. Calls `credential.get_token("https://ossrdbms-aad.database.windows.net/.default")`, and awaits the result when the credential is async.
2. Passes `token.token` as the password.
3. When neither the connection string nor `PGUSER` sets a user, takes it from the token's `upn`, `preferred_username` or `unique_name` claim, in that order. The token is decoded without verification only to read that name; the server verifies the token. If none of the claims is present, it raises `PostgresStorageError` asking you to set `user=` in the connection string.
4. Adds `sslmode=require` when neither the connection string nor `PGSSLMODE` sets an `sslmode`. Azure requires TLS.

`sslmode=require` encrypts the connection but does not check the server's certificate. To check it, set `sslmode=verify-full` and `sslrootcert=` to a file holding the root certificate authorities Microsoft lists for Azure Database for PostgreSQL, in the connection string or through `PGSSLMODE` and `PGSSLROOTCERT`.

A credential is any object with a `get_token(*scopes)` method that returns an object with `token` and `expires_on`, sync or async. Every `azure.identity` and `azure.identity.aio` credential fits, and so does your own. The package calls a synchronous `get_token` in a worker thread, so it does not block the event loop, but the `azure.identity.aio` credentials are the better fit for an async application. Close an `aio` credential when you are done with it, for example with `async with` as above.

## When sign-in fails

Before the owned pool opens, the package fetches one token and resolves the user the way a new connection will. That happens in `open()`, when you enter the hub with `async with`, or on the first call. If the credential fails, or the token names no user and none is set, that call raises `PostgresStorageError` at once. Exceptions defined in an `azure.` module keep their type and diagnostic text, for example `ClientAuthenticationError: az login required`. Other exceptions show only their type and `see the chained cause`. The original exception remains chained as the cause. Every new connection fetches its own token.

Once the pool is open, it signs in new connections in background workers, which retry instead of raising. If the credential fails there, for example after an Azure CLI login expires, the package logs the failure at error level. The record comes from `agent_framework_community_postgres._entra` and propagates to the `agent_framework_community_postgres` logger, so configuring either works. A call that then waits out the pool timeout raises `PostgresStorageError("No PostgreSQL connection became available within 10 seconds: ...")` ending in `Last connection error:` and the same failure. A token the server rejects fails like a wrong password, and psycopg_pool logs it on the `psycopg.pool` logger.

The package never adds the returned access token to its error messages, logs or `repr`. For exceptions outside the `azure.` namespace, their text is also omitted from the package's error messages and logs. Azure SDK diagnostic text is retained. The original chained cause is unchanged, so its text can still appear when an application prints the full traceback.

## Token lifetime

azure-identity caches the token and refreshes it before it expires, so asking for a token on every new connection does not reach Entra ID every time. PostgreSQL checks the password only at login. A connection that is already open stays signed in after its token expires and keeps working until it closes.

The pool's `max_lifetime` bounds how long that can be. The owned pool keeps psycopg_pool's default of one hour, after which it replaces the connection and the new one signs in with a current token.

## PgBouncer

This has not been tested behind PgBouncer. The token is an ordinary password to libpq, so a PgBouncer that passes the client's password through to the server needs nothing more from this package. The README's note on prepared statements behind PgBouncer still applies.

## The CLI

The CLI signs in with whatever password the connection string holds; it has no `credential` option. To run it with Entra ID, put a token in the password yourself, for example from the Azure CLI:

```bash
TOKEN=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)
POSTGRES_CONNECTION_STRING="host=my-server.postgres.database.azure.com dbname=agent_framework user=ada@contoso.com sslmode=require password=$TOKEN" \
  python -m agent_framework_community_postgres migrate
```

Or run `migrate --print` and hand the SQL to a DBA.

`samples/azure_entra.py` is a runnable script. It reads `POSTGRES_CONNECTION_STRING` and uses `DefaultAzureCredential`.
