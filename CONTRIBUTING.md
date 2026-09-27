# Contributing

## Run the database

The tests use the PostgreSQL service in `docker-compose.yml`. It listens on port 5433 and runs PostgreSQL 17 unless you override it.

```bash
docker compose up -d --wait
POSTGRES_VERSION=16 POSTGRES_PORT=5434 docker compose up -d --wait  # another version or port
```

## Run the checks

```bash
export POSTGRES_TEST_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

In PowerShell, set the variable with `$env:POSTGRES_TEST_CONNECTION_STRING = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"`. Without it, the integration and conformance tests are skipped. Each integration test creates its own schema and drops it afterwards.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/). Write `type(scope): subject`, for example `fix(history): skip rows that fail to decode`. The types in use are `feat`, `fix`, `docs`, `test`, `refactor`, `ci` and `chore`.

## Private Agent Framework imports

Import private Agent Framework names only in `src/agent_framework_community_postgres/_framework.py`, and add a test for each one to `tests/unit/test_framework.py`. Every other module imports from `_framework.py` or from MAF's public API.

## Add a migration

1. Write a function that takes `TableNames` and returns its statements, and append it to `MIGRATIONS` in `_migrations.py`. Its version is its position plus one.
2. Never edit a migration that has shipped in a release. Fix it with a new one.
3. If the migration adds a table or index name longer than `history_messages_session_idx`, set `_LONGEST_IDENTIFIER` in `_client.py` to it, so `TableNames` still rejects a prefix that would push a name past 63 bytes.
4. Add a test to `tests/integration/test_migrations.py`.

## Add a store

1. Add its table in a new migration in `_migrations.py`.
2. Write the module, `_<name>.py`, with a class that extends `BaseStore` and has a `purge()`. Export it from `__init__.py` and add a factory to `PostgresPersistence`, including its table in `PostgresPersistence.purge()`.
3. Add an integration test under `tests/integration/`.
4. When MAF has an in-memory equivalent, add a conformance test under `tests/conformance/` that runs the same cases against both.
5. Add a page under `docs/`, link it from `README.md`, and add a sample under `samples/`.
