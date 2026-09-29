# Compatibility

## Versions

| Dependency | Supported |
|---|---|
| Python | 3.11, 3.12, 3.13, 3.14 |
| PostgreSQL | 16, 17 |
| `agent-framework-core` | `>=1.19.0,<2` |
| `agent-framework-ag-ui` | `>=1.4.0,<2`, through the `ag-ui` extra |
| `psycopg[binary,pool]` | `>=3.3.5,<4` |

## Private Agent Framework imports

MAF does not export the three names below. Microsoft's own Cosmos DB and Redis packages import the same names from the same modules. This package imports them in `src/agent_framework_community_postgres/_framework.py` and nowhere else.

| Name | Module | Used for | Verified on |
|---|---|---|---|
| `filter_new_messages` | `agent_framework._sessions` | Dropping replayed messages in `PostgresHistoryProvider` | `agent-framework-core` 1.19.0 |
| `encode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Encoding checkpoints in `PostgresCheckpointStorage` | `agent-framework-core` 1.19.0 |
| `decode_checkpoint_value` | `agent_framework._workflows._checkpoint_encoding` | Decoding checkpoints and the save-time restore check | `agent-framework-core` 1.19.0 |

`tests/unit/test_framework.py` guards all three. It checks the names exist, that the checkpoint encoding round-trips `datetime` and `UUID` values, and that `filter_new_messages` drops a replayed prefix. If an upstream release removes a name, importing the package raises `ImportError` naming the installed `agent-framework-core` version and the supported range. CI runs the suite a second time against the newest `agent-framework-core`, and that job does not block a merge.

## JSONB limits

Every payload is stored as PostgreSQL `jsonb`, which changes a few values.

- A string or key containing the NUL character (`\u0000`) cannot be stored. Every store refuses it with `ValueError` before any SQL runs.
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
