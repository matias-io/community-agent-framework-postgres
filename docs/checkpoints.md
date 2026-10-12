# Checkpoints

`PostgresCheckpointStorage` implements MAF's `CheckpointStorage` protocol for workflows. It stores one row per checkpoint in `af_checkpoints`.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `application_id` | `text` | Part of the primary key |
| `tenant_id` | `text` | The store's `tenant_id`, or `''`. Part of the primary key |
| `scope` | `text` | The store's `scope`, or `''`. Part of the primary key |
| `payload_format` | `text` | `agent-framework.python.checkpoint.v1`, the encoding of `payload`. The store reads only rows in this format |
| `workflow_name` | `text` | `WorkflowCheckpoint.workflow_name` |
| `checkpoint_id` | `text` | Part of the primary key |
| `previous_checkpoint_id` | `text` | The parent checkpoint, if any |
| `checkpoint_timestamp` | `timestamptz` | `WorkflowCheckpoint.timestamp`, kept for inspection. Ordering does not use it |
| `sequence` | `bigint` | An identity column set on first write. Listing and `get_latest` order by it |
| `payload` | `jsonb` | The checkpoint, encoded as MAF encodes it |
| `created_at` | `timestamptz` | First write time |
| `expires_at` | `timestamptz` | Set when retention is on |

## Constructor

```python
PostgresCheckpointStorage(*, application_id, tenant_id=None, scope=None, allowed_checkpoint_types=None, ...)
```

| Argument | Default | Meaning |
|---|---|---|
| `application_id` | required | Part of the row key |
| `tenant_id` | `None` | Keeps one tenant's checkpoints apart from another's. `''` raises `ValueError` |
| `scope` | `None` | Keeps one workflow's checkpoints apart, for example per conversation or response. `''` raises `ValueError` |
| `allowed_checkpoint_types` | `None` | Extra types the decoder may restore, as `"module:QualifiedName"` strings |

The connection arguments and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use). On the hub, call `hub.checkpoint_storage(tenant_id=..., scope=..., allowed_checkpoint_types=...)`.

## Encoding

Checkpoints use the same hybrid JSON and restricted pickle encoding as MAF's `FileCheckpointStorage` and `CosmosCheckpointStorage`. Values JSON cannot hold are pickled. The decoder only unpickles:

- MAF's built-in safe value types (primitives, collections, `datetime`, `uuid` and the like) and `getattr`, which pickle uses to rebuild nested types and enum members;
- any top-level class in a module under `agent_framework.`, `agent_framework_orchestrations.` or `openai.types.`;
- every type registered process-wide with `register_checkpoint_type`;
- the types you list in `allowed_checkpoint_types`.

### Trust boundary

Any database principal with `INSERT` or `UPDATE` on `af_checkpoints` can write a row for any `application_id`, and so can inject objects into every application that reads the table. MAF documents its restricted unpickler as defence in depth, not a security boundary. Grant write access on the table only to the application's own role.

## Behaviour

- `save` encodes the checkpoint and decodes it again under this store's allowed types before it writes. A checkpoint that would not restore raises `WorkflowCheckpointException` and nothing is written. MAF's `FileCheckpointStorage` does the same check.
- Rows are keyed by `application_id`, `tenant_id`, `scope` and `checkpoint_id`. Without `tenant_id` and `scope`, every instance with the same `application_id` shares checkpoints for a `workflow_name`. MAF's protocol only knows `workflow_name`. When a workflow runs once per conversation, pass the conversation id as `scope`.
- Saving an existing `checkpoint_id` again replaces its content and keeps its position in the order, as MAF's in-memory and file storage do.
- Saves in one `application_id`, `tenant_id` and `scope` take turns on a transaction-scoped advisory lock, so `sequence` order is commit order.
- A timestamp without a time zone is read as UTC. A timestamp `datetime.fromisoformat` cannot parse raises `WorkflowCheckpointException`.
- `list_checkpoints` and `list_checkpoint_ids` return checkpoints in write order, oldest first. `get_latest` returns the last checkpoint written, whatever clock produced its `timestamp`.
- `load(checkpoint_id)` raises `WorkflowCheckpointException` when the id is absent in this scope or its row fails to decode. `get_latest` raises the same when the newest row fails to decode.
- `list_checkpoints` skips a row that fails to decode and logs a warning. `list_checkpoint_ids` does not decode, so it returns every id, and a broken row can be found and deleted.
- Messages and log lines name no checkpoint id or workflow name.
- `delete(checkpoint_id)` returns `True` when a row existed.

## Differences from MAF's own stores

1. Ordering. `InMemoryCheckpointStorage` and `FileCheckpointStorage` pick the latest checkpoint by `checkpoint.timestamp` (`agent-framework-core` 1.20, `_workflows/_checkpoint.py`). This store picks by write order. On one host the two agree. Across hosts whose clocks differ, write order is the one that names the checkpoint written last.
2. A corrupt newest checkpoint. `FileCheckpointStorage` skips a file that fails to decode, so `get_latest` resumes from an older checkpoint. This store raises `WorkflowCheckpointException` from `get_latest` and `load`, because silently resuming from stale state is worse than failing. `list_checkpoints` still skips the row, and `list_checkpoint_ids` returns every id so the row can be deleted.
3. Upstream PR [microsoft/agent-framework#8664](https://github.com/microsoft/agent-framework/pull/8664) proposes a `PostgresCheckpointStorage` in `agent-framework-postgres` with immutable rows, scoped by `run_id`. This store overwrites a re-saved id as the shipped stores do, and scopes by `scope`. The table layouts share ideas (`tenant_id`, `payload_format`, `sequence`) but are not the same table.

## Example

```python
import asyncio
import sys

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from agent_framework_community_postgres import PostgresPersistence

DSN = "postgresql://postgres:postgres@127.0.0.1:5433/agent_framework"


class Upper(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str]) -> None:
        await ctx.send_message(text.upper())


class Finish(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str, str]) -> None:
        await ctx.yield_output(text + "!")


async def main() -> None:
    async with PostgresPersistence(application_id="docs", connection_string=DSN) as hub:
        await hub.migrate()
        storage = hub.checkpoint_storage(scope="conversation-1")
        upper, finish = Upper(id="upper"), Finish(id="finish")
        workflow = WorkflowBuilder(start_executor=upper, checkpoint_storage=storage).add_edge(upper, finish).build()
        result = await workflow.run("hello")
        print(result.get_outputs())
        print(len(await storage.list_checkpoint_ids(workflow_name=workflow.name)), "checkpoints")


with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
    runner.run(main())
```

## Retention

Each checkpoint is its own row, and `save` sets its `expires_at` to now plus the TTL. A later checkpoint does not extend an earlier one. Saving the same `checkpoint_id` again resets that row's expiry. `purge()` deletes expired rows one by one, so a long-running workflow loses its oldest checkpoints first. `purge()` covers this store's scope. Checkpoints are always deleted, never tombstoned, because a partial checkpoint cannot be resumed. See [retention.md](retention.md).
