# Checkpoints

`PostgresCheckpointStorage` implements MAF's `CheckpointStorage` protocol for workflows. It stores one row per checkpoint in `af_checkpoints`.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `application_id` | `text` | Part of the primary key |
| `scope` | `text` | The store's `scope`, or `''`. Part of the primary key |
| `workflow_name` | `text` | `WorkflowCheckpoint.workflow_name` |
| `checkpoint_id` | `text` | Part of the primary key |
| `previous_checkpoint_id` | `text` | The parent checkpoint, if any |
| `checkpoint_timestamp` | `timestamptz` | `WorkflowCheckpoint.timestamp` |
| `iteration_count` | `integer` | `WorkflowCheckpoint.iteration_count` |
| `encoded` | `jsonb` | The checkpoint, encoded as MAF encodes it |
| `created_at` | `timestamptz` | Write time |
| `expires_at` | `timestamptz` | Set when retention is on |

## Constructor

```python
PostgresCheckpointStorage(*, application_id, scope=None, allowed_checkpoint_types=None, ...)
```

| Argument | Default | Meaning |
|---|---|---|
| `application_id` | required | Part of the row key |
| `scope` | `None` | Keeps one workflow's checkpoints apart, for example per conversation. `''` raises `ValueError` |
| `allowed_checkpoint_types` | `None` | Extra types the decoder may restore, as `"module:QualifiedName"` strings |

The connection arguments and `retention` are the same on every store. See [Standalone use](../README.md#standalone-use). On the hub, call `hub.checkpoint_storage(scope=..., allowed_checkpoint_types=...)`.

## Encoding

Checkpoints use the same hybrid JSON and restricted pickle encoding as MAF's `FileCheckpointStorage` and `CosmosCheckpointStorage`. Values JSON cannot hold are pickled, so treat the database as a trust boundary. The decoder only unpickles MAF's built-in safe types, MAF's own types and the types you list in `allowed_checkpoint_types`.

## Behaviour

- `save` encodes the checkpoint and decodes it again under this store's allowed types before it writes. A checkpoint that would not restore raises `WorkflowCheckpointException` and nothing is written. MAF's `FileCheckpointStorage` 1.19 does the same check.
- Without `scope`, every instance with the same `application_id` shares checkpoints for a `workflow_name`. MAF's protocol only knows `workflow_name`. When a workflow runs once per conversation, pass the conversation id as `scope`.
- Saving an existing `checkpoint_id` again replaces the row, as MAF's in-memory and file storage do.
- A timestamp without a time zone is read as UTC. A timestamp `datetime.fromisoformat` cannot parse raises `WorkflowCheckpointException`.
- `load(checkpoint_id)` raises `WorkflowCheckpointException` when the id is absent in this scope. Its messages and log lines name no checkpoint id or workflow name.
- `list_checkpoints` and `list_checkpoint_ids` return oldest first. `get_latest` returns the newest by checkpoint timestamp, then by write time.
- `list_checkpoints`, `list_checkpoint_ids` and `get_latest` skip a row that fails to decode and log a warning without its id. `get_latest` returns the newest row that decodes. `load` raises for a row that fails to decode.
- `delete(checkpoint_id)` returns `True` when a row existed.

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
