# Compatibility

## Versions

| Package | `agent-framework-core` | `agent-framework-ag-ui` (`ag-ui` extra) | Python | PostgreSQL | psycopg |
|---|---|---|---|---|---|
| 0.1.1 | Declared `>=1.20.0,<2`; tested on 1.20.0 | Declared `>=1.5.0,<2`; tested on 1.5.0 | 3.11 to 3.14 | 16, 17 | Declared `>=3.3.5,<4`; tested on 3.3.6 |

0.1.1 is the first release on PyPI. 0.1.0 was never published.

The `azure` extra installs `azure-identity>=1.19,<2` and `aiohttp>=3.9,<4` for [Microsoft Entra ID sign-in](azure-entra.md).

Python 3.15 is not supported yet. The samples and the test suite passed on 3.15.0b4 with `agent-framework-core` 1.20.0, but only a beta was tested, so the package does not claim it.

## Support policy

The package declares two ranges and tests a narrower one.

- **Declared**: `agent-framework-core>=1.20.0,<2` and, through the `ag-ui` extra, `agent-framework-ag-ui>=1.5.0,<2`. pip and uv enforce these.
- **Tested**: `agent-framework-core` 1.20.0 and `agent-framework-ag-ui` 1.5.0. The lock file pins these, the lowest supported versions, and CI also runs the suite on the newest release the declared range allows.

The declared range assumes Agent Framework follows semantic versioning and keeps breaking changes for major versions. Microsoft's own integrations make the same assumption: `agent-framework-azure-cosmos` and `agent-framework-redis` declare `agent-framework-core>=1.19.0,<2`. This package starts at 1.20 instead, for the reason under [Behaviour MAF owns](#behaviour-maf-owns). Microsoft releases its packages in lockstep with MAF. This package cannot, and MAF ships a minor every one to two weeks, so a range that listed only tested minors would block every new MAF release until a patch release here caught up. Instead, a weekly canary workflow runs the suite against the newest MAF release, and each minor that passes is added to the tested range in the next release.

### The compatibility check

`compatibility()` returns a `Compatibility` with the installed and tested versions. `core_installed` and `core_tested` cover `agent-framework-core`, and `ag_ui_installed` and `ag_ui_tested` cover `agent-framework-ag-ui`; `ag_ui_installed` is `None` when the `ag-ui` extra is not installed. `untested` names each distribution whose installed major.minor is newer than the newest tested one, and `out_of_range` names each one whose major is newer than the tested major. Both are empty when every installed version is covered. Older and tested versions are never untested. Pre-release and local version parts are ignored: only the numeric major.minor is compared.

```python
from agent_framework_community_postgres import compatibility

report = compatibility()
print(report.core_installed, report.core_tested, report.untested)
```

The first store or `PostgresPersistence` built in a process logs one warning for each untested distribution on the `agent_framework_community_postgres` logger, naming the installed and tested versions. Later stores log nothing. Importing the package has no side effects, so `python -W error` and a pytest `filterwarnings = ["error"]` keep working on any version the declared range allows.

For a newer minor, the warning means the combination has not been run here, not that it is known to fail, and it is expected to work. For a new major, which pip and uv only install when forced, the warning says it is outside the declared range and likely to break.

### If a newer MAF breaks something

Pin the last tested minor, for example `agent-framework-core<1.21`, and open an issue at <https://github.com/matias-io/community-agent-framework-postgres/issues> with the MAF version and the error. A fix ships in a patch release.

### Behaviour MAF owns

Behaviour that MAF owns follows the installed MAF version. `PostgresHistoryProvider` drops replayed messages with MAF's own `filter_new_messages`, so the replay rule is MAF's. Since 1.20 it drops only a replayed overlap between the stored history and the incoming batch, and a message the user repeats, such as a second "yes", is stored.

MAF 1.19 also dropped any incoming message whose role and content matched a message stored anywhere earlier in the session, so a repeated "yes" was lost. Microsoft's Cosmos DB and Redis history providers use the same function and behave the same way on 1.19. That is why this package requires 1.20.

## Private Agent Framework imports

MAF does not export the three names below, from two private modules. Microsoft's own Cosmos DB package imports all three from the same modules, and its Redis package imports `filter_new_messages`. This package imports them in `src/agent_framework_community_postgres/_framework.py` and nowhere else.

| Name | Module | Used for | Verified on |
|---|---|---|---|
| `filter_new_messages` | `agent_framework._sessions` | Dropping replayed messages in `PostgresHistoryProvider` | `agent-framework-core` 1.20.0 |
| `encode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Encoding checkpoints in `PostgresCheckpointStorage` | `agent-framework-core` 1.20.0 |
| `decode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Decoding checkpoints and the save-time restore check | `agent-framework-core` 1.20.0 |

`tests/unit/test_framework.py` guards all three. It checks the names exist, that the checkpoint encoding round-trips `datetime` and `UUID` values, and that `filter_new_messages` drops a replayed prefix. If an upstream release removes a name, importing the package raises `ImportError` naming the installed `agent-framework-core` version, the declared range and the tested minors, and asking you to pin an earlier version and open an issue. CI runs the suite a second time against the newest `agent-framework-core` the declared range allows, and that job blocks a merge. The weekly canary runs it against the newest MAF release, so a rename in a new MAF minor shows up there first.

## JSONB limits

Every payload is stored as PostgreSQL `jsonb`, which changes a few values.

- A string or key containing the NUL character (`\u0000`) cannot be stored. Every store refuses it with `ValueError` before any SQL runs.
- A string or key containing a lone surrogate (for example `"\ud800"`) is not valid UTF-8. Every store refuses it with `ValueError` before any SQL runs.
- A float of about `1e16` or more comes back as an `int`.
- `-0.0` comes back as `0.0`.
- `nan` and infinity are not JSON. Every store refuses them with `ValueError` before any SQL runs, as it does a circular reference or any value `json` cannot encode.
- These errors name the field, for example `Session state`, and the Python error type. They never quote the value.

Ordinary strings, integers, floats, booleans, lists and objects round-trip exactly.

## Windows

Async psycopg cannot run on Windows' default `ProactorEventLoop`. Start a script on a selector loop with Python 3.11 or later:

```python
with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

Run uvicorn 0.36 or later with `--loop asyncio:SelectorEventLoop`. uvicorn imports that value as the loop factory. Its plain `--loop asyncio` picks `ProactorEventLoop` on Windows unless reload or workers are on. The CLI and the samples already use a selector loop.

Use `127.0.0.1` rather than `localhost` in local connection strings. libpq tries `::1` first, and some Windows and WSL setups drop that traffic without an error.
