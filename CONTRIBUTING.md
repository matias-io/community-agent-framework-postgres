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

`ci.yml` runs the locked MAF versions on Python 3.11 to 3.14 against PostgreSQL 16 and 17. It also runs the newest MAF release the declared ranges allow on each Python version against PostgreSQL 17. All these jobs block a merge. `canary.yml` runs every Monday against the newest MAF release, ignoring the untested-version warning. When it passes on a new minor, add that minor to `TESTED_CORE` or `TESTED_AG_UI` in `_framework.py` and to `docs/compatibility.md` in the next release; when it fails, the fix ships in a patch release. GitHub disables a scheduled workflow after 60 days without repository activity, so check that the canary is still enabled. `release.yml` runs the full suite against PostgreSQL 17 before it builds, and publishes only if that passes.

## Releasing

A release goes to PyPI from a tag, through `release.yml`. PyPI does not allow an uploaded distribution filename to be reused, even after that file is deleted, so check everything before the tag is pushed.

One-time setup:

1. On PyPI, add a pending trusted publisher for the project `community-agent-framework-postgres`: owner `matias-io`, repository `community-agent-framework-postgres`, workflow `release.yml`, environment `pypi`. After the first upload it becomes the project's trusted publisher.
2. On GitHub, create the environment `pypi` under Settings, Environments. Adding yourself as a required reviewer makes every publish wait for approval.

For each release:

1. On the release branch, set `project.version` in `pyproject.toml`, add the version's section and link reference to `CHANGELOG.md`, replace its `Unreleased` date with the publication date, update the tested versions in `_framework.py` and `docs/compatibility.md`, and point the README's links at the new tag (`blob/vX.Y.Z/`); `tests/unit/test_docs.py` checks the tag matches `project.version`.
2. Push the branch and open a pull request against `main`.
3. Wait for the full CI matrix to pass, including PostgreSQL 16 and the `latest` MAF leg.
4. Merge the pull request.
5. Create an annotated tag on the merge commit that matches `project.version`, for example `git tag -a v0.1.1 -m "v0.1.1"`.
6. Do not push the local `v0.1.0` tag. `release.yml` at that commit publishes on any `v*` tag, so pushing it would publish 0.1.0 to PyPI. Push only the new release tag.
7. Push the tag: `git push origin v0.1.1`. This starts `release.yml`, which checks the tag against `project.version`, runs the full suite against PostgreSQL 17, builds, and publishes.
8. Open the project page on PyPI and check the README renders: links resolve to the tag on GitHub, and the tables display.

Optional dry run on TestPyPI before step 7: register the same pending publisher on test.pypi.org, then build locally with `uv build` and upload with `uv publish --publish-url https://test.pypi.org/legacy/` using a TestPyPI token, or add a job with `repository-url: https://test.pypi.org/legacy/` to the publish action. A version on TestPyPI cannot be reused either.
