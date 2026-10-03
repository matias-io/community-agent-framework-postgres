# Compatibility

## Versions

| Package | `agent-framework-core` | `agent-framework-ag-ui` | Python | PostgreSQL | psycopg |
|---|---|---|---|---|---|
| 0.1.1 | 1.19.x to 1.20.x (`>=1.19.0,<1.21`) | 1.4.x to 1.5.x (`>=1.4.0,<1.6`, `ag-ui` extra) | 3.11 to 3.14 | 16, 17 | 3.3 and later (`>=3.3.5,<4`) |
| 0.1.0 | `>=1.19.0,<2` | `>=1.4.0,<2` (`ag-ui` extra) | 3.11 to 3.14 | 16, 17 | `>=3.3.5,<4` |

The `azure` extra installs `azure-identity>=1.19,<2` for [Microsoft Entra ID sign-in](azure-entra.md).

Python 3.15 is not supported yet. The samples and the test suite passed on 3.15.0b4 with `agent-framework-core` 1.20.0, but only a beta was tested, so the package does not claim it.

## Support policy

The upper bounds on `agent-framework-core` and `agent-framework-ag-ui` are the range this package was tested against, and pip and uv enforce them. A MAF release outside that range will not install alongside this package until a release says it works.

Each new MAF minor is tested in CI. When it passes, a patch release widens the bound; when it needs a change, the change ships in that patch release.

Microsoft's Cosmos DB and Redis integrations accept any `agent-framework-core` below 2 (`agent-framework-azure-cosmos` declares `>=1.19.0,<2`), because Microsoft releases them in lockstep with MAF. This package does not release in lockstep, so its range is narrower: it declares only the minors it has run against.

Behaviour that MAF owns follows the installed MAF version. `PostgresHistoryProvider` drops replayed messages with MAF's own `filter_new_messages`, so the replay rule is MAF's. In 1.20 that rule changed: when the stored history is exactly one user message without an id, an incoming batch that starts with that same message is kept as a repeated input instead of being dropped as a replay. On 1.19 the repeat is dropped; on 1.20 it is stored.

## Private Agent Framework imports

MAF does not export the three names below. Microsoft's own Cosmos DB and Redis packages import the same names from the same modules. This package imports them in `src/agent_framework_community_postgres/_framework.py` and nowhere else.

| Name | Module | Used for | Verified on |
|---|---|---|---|
| `filter_new_messages` | `agent_framework._sessions` | Dropping replayed messages in `PostgresHistoryProvider` | `agent-framework-core` 1.19.0, 1.20.0 |
| `encode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Encoding checkpoints in `PostgresCheckpointStorage` | `agent-framework-core` 1.19.0, 1.20.0 |
| `decode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Decoding checkpoints and the save-time restore check | `agent-framework-core` 1.19.0, 1.20.0 |

`tests/unit/test_framework.py` guards all three. It checks the names exist, that the checkpoint encoding round-trips `datetime` and `UUID` values, and that `filter_new_messages` drops a replayed prefix. If an upstream release removes a name, importing the package raises `ImportError` naming the installed `agent-framework-core` version and the supported range. CI runs the suite a second time against the newest `agent-framework-core` the declared range allows, and that job blocks a merge. A weekly canary workflow runs it against the newest release past the upper bound, so a rename in a new MAF minor shows up there before a patch release widens the bound.

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
