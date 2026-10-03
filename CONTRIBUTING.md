# Contributing

## Run the database

The tests use the PostgreSQL service in `docker-compose.yml`. It listens on port 5433 and runs PostgreSQL 17 unless you override it.

```bash
docker compose up -d --wait
```

To test against PostgreSQL 16, start it as a separate compose project. The default project's volume holds a PostgreSQL 17 data directory, which PostgreSQL 16 refuses to start on.

```bash
POSTGRES_VERSION=16 POSTGRES_PORT=5434 docker compose -p cafp-pg16 up -d --wait
export POSTGRES_TEST_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5434/agent_framework
uv run pytest
docker compose -p cafp-pg16 down -v
```

In PowerShell:

```powershell
$env:POSTGRES_VERSION = "16"; $env:POSTGRES_PORT = "5434"; docker compose -p cafp-pg16 up -d --wait
Remove-Item Env:POSTGRES_VERSION, Env:POSTGRES_PORT
$env:POSTGRES_TEST_CONNECTION_STRING = "postgresql://postgres:postgres@127.0.0.1:5434/agent_framework"
uv run pytest
docker compose -p cafp-pg16 down -v
```

The last command stops it and deletes its volume.

## Run the checks

```bash
export POSTGRES_TEST_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:5433/agent_framework
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

In PowerShell:

```powershell
$env:POSTGRES_TEST_CONNECTION_STRING = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Without `POSTGRES_TEST_CONNECTION_STRING`, the integration and conformance tests are skipped. Each integration test creates its own schema and drops it afterwards.

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

## GitHub Actions

Every action in `.github/workflows/` is pinned by commit SHA, with the tag or branch it came from in a comment. To update one, resolve the new tag to its commit (dereference an annotated tag to the commit it points at) and change the SHA and the comment together. Dependabot (`.github/dependabot.yml`) checks the pins weekly and opens a pull request that does both.

`ci.yml` runs the suite on every Python and PostgreSQL version, once on the locked MAF versions and once on the newest MAF release the declared ranges allow. Both legs block a merge. `canary.yml` runs every Monday against the newest MAF release, past the declared upper bound. When it fails, a new MAF minor needs work before a patch release widens the bound. GitHub disables a scheduled workflow after 60 days without repository activity, so check that the canary is still enabled. `release.yml` runs the full suite against PostgreSQL 17 before it builds, and publishes only if that passes.
