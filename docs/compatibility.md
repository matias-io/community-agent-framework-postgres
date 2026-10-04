# Compatibility

## Versions

| Package | `agent-framework-core` | `agent-framework-ag-ui` (`ag-ui` extra) | Python | PostgreSQL | psycopg |
|---|---|---|---|---|---|
| 0.1.1 | Declared `>=1.19.0,<2`; tested on 1.19.x and 1.20.x | Declared `>=1.4.0,<2`; tested on 1.4.x and 1.5.x | 3.11 to 3.14 (3.15.0b4 smoke-tested, not supported yet) | 16, 17 | 3.3.5 or later, below 4 (`>=3.3.5,<4`) |
| 0.1.0 | Declared `>=1.19.0,<2`; tested on 1.19.0 only | Declared `>=1.4.0,<2` | 3.11 to 3.14 | 16, 17 | 3.3.5 or later, below 4 (`>=3.3.5,<4`) |

0.1.0 is the unpublished snapshot at commit `50c91c5`, with a local tag that must not be pushed. 0.1.1 supersedes it and is intended as the first PyPI release.

The `azure` extra installs `azure-identity>=1.19,<2` and `aiohttp>=3.9,<4` for [Microsoft Entra ID sign-in](azure-entra.md).

Python 3.15 is not supported yet. The samples and the test suite passed on 3.15.0b4 with `agent-framework-core` 1.20.0, but only a beta was tested, so the package does not claim it.

## Support policy

The package declares two ranges and tests a narrower one.

- **Declared**: `agent-framework-core>=1.19.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.4.0,<2`. pip and uv enforce these.
- **Tested**: `agent-framework-core` 1.19.x and 1.20.x, `agent-framework-ag-ui` 1.4.x and 1.5.x. The lock file keeps 1.19.0 and 1.4.0, the lowest supported versions, and CI also runs the suite on the newest release the declared range allows.

The declared range assumes Agent Framework follows semantic versioning and keeps breaking changes for major versions. Microsoft's own integrations make the same assumption: `agent-framework-azure-cosmos` and `agent-framework-redis` declare `agent-framework-core>=1.19.0,<2`. Microsoft releases those packages in lockstep with MAF. This package cannot, and MAF ships a minor every one to two weeks, so a range that listed only tested minors would block every new MAF release until a patch release here caught up. Instead, a weekly canary workflow runs the suite against the newest MAF release, and each minor that passes is added to the tested range in the next release.

### The untested-version warning

When the installed `agent-framework-core`, or `agent-framework-ag-ui` if installed, is a newer major.minor than the newest tested one, importing the package emits one `UntestedAgentFrameworkWarning` naming the installed and tested versions. Older and tested versions never warn. Pre-release and local version parts are ignored: only the numeric major.minor is compared.

The warning means the combination has not been run here, not that it is known to fail. It is expected to work. The warning fires while the package is imported, so a filter has to be in place before the first import, and it matches the message, which always starts with `community-agent-framework-postgres has not been tested`:

```python
import warnings

warnings.filterwarnings("ignore", message="community-agent-framework-postgres has not been tested")

import agent_framework_community_postgres  # noqa: E402
```

From the environment:

```bash
PYTHONWARNINGS="ignore:community-agent-framework-postgres has not been tested" python app.py
```

In pytest, add `"ignore:community-agent-framework-postgres has not been tested"` to `filterwarnings`, or pass it with `-W`. Once the package is imported, the category is `agent_framework_community_postgres.UntestedAgentFrameworkWarning`, and `warnings.filterwarnings("ignore", category=UntestedAgentFrameworkWarning)` silences any later check, for example after `importlib.reload`.

### If a newer MAF breaks something

Pin the last tested minor, for example `agent-framework-core<1.21`, and open an issue at <https://github.com/matias-io/community-agent-framework-postgres/issues> with the MAF version and the error. A fix ships in a patch release.

### Behaviour MAF owns

Behaviour that MAF owns follows the installed MAF version. `PostgresHistoryProvider` drops replayed messages with MAF's own `filter_new_messages`, so the replay rule is MAF's. In 1.20 that rule changed: when the stored history is exactly one user message without an id, an incoming batch that starts with that same message is kept as a repeated input instead of being dropped as a replay. On 1.19 the repeat is dropped; on 1.20 it is stored.

## Private Agent Framework imports

MAF does not export the three names below, from two private modules. Microsoft's own Cosmos DB and Redis packages import the same names from the same modules. This package imports them in `src/agent_framework_community_postgres/_framework.py` and nowhere else.

| Name | Module | Used for | Verified on |
|---|---|---|---|
| `filter_new_messages` | `agent_framework._sessions` | Dropping replayed messages in `PostgresHistoryProvider` | `agent-framework-core` 1.19.0, 1.20.0 |
| `encode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Encoding checkpoints in `PostgresCheckpointStorage` | `agent-framework-core` 1.19.0, 1.20.0 |
| `decode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Decoding checkpoints and the save-time restore check | `agent-framework-core` 1.19.0, 1.20.0 |

`tests/unit/test_framework.py` guards all three. It checks the names exist, that the checkpoint encoding round-trips `datetime` and `UUID` values, and that `filter_new_messages` drops a replayed prefix. If an upstream release removes a name, importing the package raises `ImportError` naming the installed `agent-framework-core` version, the declared range and the tested minors, and asking you to pin an earlier version and open an issue. CI runs the suite a second time against the newest `agent-framework-core` the declared range allows, and that job blocks a merge. The weekly canary runs it against the newest MAF release, so a rename in a new MAF minor shows up there first.

## JSONB limits

Every payload is stored as PostgreSQL `jsonb`, which changes a few values.

- A string or key containing the NUL character (`\u0000`) cannot be stored. Every store refuses it with `ValueError` before any SQL runs.
- A string or key containing a lone surrogate (for example `"\ud800"`) is not valid UTF-8. Every store refuses it with `ValueError` before any SQL runs.
- A float of about `1e16` or more comes back as an `int`.
- `-0.0` comes back as `0.0`.
- `nan` and infinity are not JSON. Every store refuses them with `ValueError` before any SQL runs, as it does a circular reference or any value `json` cannot encode.
- These errors name the field, for example `Document payload`, and the Python error type. They never quote the value.

Ordinary strings, integers, floats, booleans, lists and objects round-trip exactly.

## Windows

Async psycopg cannot run on Windows' default `ProactorEventLoop`. Start a script on a selector loop with Python 3.11 or later:

```python
with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Run uvicorn 0.36 or later with `--loop asyncio:SelectorEventLoop`. uvicorn imports that value as the loop factory. Its plain `--loop asyncio` picks `ProactorEventLoop` on Windows unless reload or workers are on. The CLI and the samples already use a selector loop.

Use `127.0.0.1` rather than `localhost` in local connection strings. libpq tries `::1` first, and some Windows and WSL setups drop that traffic without an error.
