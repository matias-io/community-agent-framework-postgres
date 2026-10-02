# Microsoft Entra ID on Azure Database for PostgreSQL

Pass `credential=` and the pool this package creates signs in to Azure Database for PostgreSQL with a Microsoft Entra ID token instead of a password. It plays the role of the `credential=` argument on Microsoft's Cosmos DB integration and follows Microsoft's documented psycopg 3 pattern: a connection class whose `connect` fetches a token and passes it as the password.

Verified with a fake credential against PostgreSQL 16 and 17; not yet run against an Azure server.

## Prerequisites

- An Azure Database for PostgreSQL flexible server with Microsoft Entra authentication enabled.
- The Entra user, group, service principal or managed identity added to the server as a database role, with the privileges `migrate()` needs on the schema. See [migrations.md](migrations.md).
- The `azure` extra, which installs `azure-identity`:

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

On Azure compute, a managed identity is the usual choice. Its token carries no user name, so set `user=` to the database role you created for it, which is normally the identity's name:

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
3. When the connection string has no `user`, takes it from the token's `upn`, `preferred_username` or `unique_name` claim, in that order. The token is decoded without verification only to read that name; the server verifies the token. If none of the claims is present, it raises `PostgresStorageError` asking you to set `user=` in the connection string.
4. Adds `sslmode=require` when the connection string sets no `sslmode`. Azure requires TLS.

A credential is any object with a `get_token(*scopes)` method that returns an object with `token` and `expires_on`, sync or async. Every `azure.identity` and `azure.identity.aio` credential fits, and so does your own. When the credential fails, the package raises `PostgresStorageError` naming the scope and chains the credential's error as the cause. No message, log line or `repr` from this package contains the token.

Prefer the `azure.identity.aio` credentials in an async application: a synchronous credential blocks the event loop while it fetches or refreshes a token. Close an `aio` credential when you are done with it, for example with `async with` as above.

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
