from agent_framework_ag_ui import AGUIThreadSnapshotStore

from agent_framework_community_postgres import PostgresAGUIThreadSnapshotStore


async def test_the_store_satisfies_the_ag_ui_protocol() -> None:
    # pyright checks only src, so the static check lives in _thread_snapshot_store.py; this is the runtime half.
    postgres = PostgresAGUIThreadSnapshotStore(application_id="a", connection_string="host=127.0.0.1 user=app")
    try:
        store: AGUIThreadSnapshotStore = postgres
        assert isinstance(store, AGUIThreadSnapshotStore)
    finally:
        await postgres.close()
