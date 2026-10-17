# Connections

Every store and `PostgresPersistence` connect the same way. Most applications build one `PostgresPersistence` and let it own a single pool for every store.

## Arguments

- `connection_string` or `client`. Pass at most one. When both are absent, the connection string comes from `POSTGRES_CONNECTION_STRING`, read through MAF's `load_settings`. An explicit argument wins over the `.env` file named by `env_file_path`, which wins over the environment.
- `client` is a psycopg `AsyncConnection` or `AsyncConnectionPool`. A pool that is not open is opened on first use. The package never closes a client it did not create.
- `credential` signs the owned pool in to Azure Database for PostgreSQL with Microsoft Entra ID tokens. It needs a connection string and cannot be combined with `client`. With a credential, the pool defaults to `sslmode=verify-full` with `sslrootcert=system`. An `sslmode` or `sslrootcert` set in `pool_kwargs["kwargs"]`, the connection string, `PGSSLMODE` or `PGSSLROOTCERT` takes precedence over these defaults, and the store or hub raises `ValueError` when it is created if the `sslmode` in effect is `disable`, `allow` or `prefer`, and logs a warning for `require` or `verify-ca`, which encrypt without fully verifying the server. An explicit `sslmode` also drops the `sslrootcert=system` default. On Windows, `system` finds no certificates in psycopg's binary wheel, so set `PGSSLROOTCERT` to a CA file; [azure-entra.md](azure-entra.md) says which.
- A single `AsyncConnection` runs one call at a time. Every store and hub over it waits for the same lock, so a server should pass a pool. On a connection already inside your own transaction, writes and multi-statement calls become a savepoint, single-statement reads run directly inside your transaction, and transaction-scoped locks (the history save lock and the migration lock) are held until your transaction ends. Your transaction's isolation level applies there too. At `REPEATABLE READ` or `SERIALIZABLE`, a history save reads a snapshot taken before it waited for the session lock, so two such transactions saving one session can store an overlapping message twice (`REPEATABLE READ`) or fail with a serialization error (`SERIALIZABLE`). The same applies to a history purge run inside such a transaction: its check that no message is live reads your snapshot, so it cannot see a save that committed after your transaction began. Use `READ COMMITTED`, PostgreSQL's default, for transactions that save or purge history.
- A connection string makes the store own a pool with `min_size=1`, `max_size=10`, autocommit, libpq `connect_timeout=10` unless the string sets one, and a pool `timeout` of 10 seconds.
- The owned pool also sets TCP keepalives (`keepalives=1`, `keepalives_idle=30`, `keepalives_interval=10`, `keepalives_count=3`) and `tcp_user_timeout=30000` unless the connection string sets them, so a silently dead connection fails within about a minute. The pool sets no statement or lock timeout; `migrate()` sets its own lock timeout.
- `pool_kwargs` is merged over the owned pool's defaults, and the `kwargs` entry inside it over the connection defaults; for example `{"max_size": 50, "kwargs": {"prepare_threshold": None}}` for PgBouncer. A connection parameter in `kwargs` wins over the same parameter in the connection string. It cannot be combined with `client`.
- `schema="public"` must be a lowercase identifier that does not start with `pg_`. `table_prefix="af_"` allows lowercase letters, digits and underscores, up to 28 bytes.

When the database is down or rejects the login, or every pooled connection stays busy, each call raises `PostgresStorageError("No PostgreSQL connection became available within 10 seconds: ...")` after about 10 seconds. psycopg_pool raises the same timeout in both cases, so the message names both. Over your own pool, the message gives that pool's `timeout`. The owned pool keeps retrying in the background and recovers when the database returns. psycopg_pool logs the driver's reason on the `psycopg.pool` logger. If you need other timeouts, set them through `pool_kwargs` or pass your own pool. A malformed connection string raises `PostgresStorageError("Invalid connection string.")` and never repeats libpq's text, which can contain the password.

A driver failure raises `PostgresStorageError` with the driver exception chained as the cause. Its `sqlstate` is the server's error code when there was one. Its `retryable` is `True` for SQLSTATE classes `08` (connection exceptions), `40` (serialization failures and deadlocks), `53` (insufficient resources, such as too many connections), codes `57P0x` (operator intervention, such as an administrator shutdown) and `55P03` (a lock timeout), for a connection lost without a SQLSTATE, and for the pool timeout above. A fresh call may get past these. An error on a borrowed `AsyncConnection` that has closed is never retryable, because a retry would reach the same dead connection.

After a database restart or failover, each stale connection in the owned pool fails one call before the pool replaces it. Behind PgBouncer in transaction mode older than 1.21, or without `max_prepared_statements`, psycopg's prepared statements fail, because psycopg prepares a query after its fifth run. For either case, pass your own pool. `check=AsyncConnectionPool.check_connection` tests each connection as the pool hands it out, at the cost of one round trip, and `prepare_threshold=None` turns prepared statements off.

```python
import asyncio
import sys

from psycopg_pool import AsyncConnectionPool

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


async def main() -> None:
    pool = AsyncConnectionPool(
        DSN,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=AsyncConnectionPool.check_connection,
    )
    async with pool, PostgresPersistence(application_id="my-app", client=pool) as hub:
        await hub.migrate()
        print(await hub.pending_migrations())


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

## Sizing the pool

The owned pool has `max_size=10`. A history save holds its connection while it waits for the session's lock, and a checkpoint save while it waits for its scope's lock, so writers that queue on one session or one scope also queue for connections. Set `pool_kwargs={"max_size": ...}` to at least the number of calls you expect to run at once. Each store built from a connection string owns its own pool, so build stores once at startup, or from one hub, rather than per request.

Checkpoint saves in one scope take turns, which keeps "latest" in write order. Without a `scope`, every checkpoint save of the application shares one lock, so give each workflow run its own scope, such as a conversation or order id, when runs happen in parallel.
