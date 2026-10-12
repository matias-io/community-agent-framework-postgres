import re
from datetime import timedelta
from typing import cast

import pytest

from agent_framework_community_postgres._client import ClientHandle, TableNames
from agent_framework_community_postgres._migrations import MIGRATIONS, migrate, render


def test_render_lists_every_table_with_quoted_names() -> None:
    text = render(TableNames(schema="agents", prefix="af_"))
    for table in (
        "migrations",
        "history_messages",
        "sessions",
        "checkpoints",
        "thread_snapshots",
        "leases",
    ):
        assert f'"agents"."af_{table}"' in text
    assert "-- version 1" in text
    assert text.count("CREATE TABLE") == 6


def test_render_can_select_versions() -> None:
    assert render(TableNames(), versions=[]) == ""
    assert len(MIGRATIONS) == 1


def test_every_identifier_fits_at_the_longest_allowed_prefix() -> None:
    # TableNames accepts a prefix up to the length that keeps its longest known identifier within 63 bytes.
    # If a migration adds a longer table or index name, this fails until _client._LONGEST_IDENTIFIER is updated.
    longest_ok = "p" * 28
    text = render(TableNames(prefix=longest_ok))
    for name in re.findall(r'"([^"]+)"', text):
        assert len(name.encode("utf-8")) <= 63, name


def test_render_wraps_each_version_in_one_transaction() -> None:
    text = render(TableNames())
    assert text.count("BEGIN;") == len(MIGRATIONS)
    assert text.count("COMMIT;") == len(MIGRATIONS)
    assert text.index("BEGIN;") > text.index("-- version 1")
    assert text.index("COMMIT;") > text.index("INSERT INTO")


@pytest.mark.parametrize("versions", [[0], [2], [-1], [1, 2]])
def test_render_rejects_unknown_versions(versions: list[int]) -> None:
    with pytest.raises(ValueError, match="between 1 and 1"):
        render(TableNames(), versions=versions)


def test_there_is_one_schema_version() -> None:
    assert len(MIGRATIONS) == 1


def test_render_from_version_prints_only_later_versions() -> None:
    assert render(TableNames(), versions=range(2, len(MIGRATIONS) + 1)) == ""
    assert "-- version 1" in render(TableNames(), versions=range(1, len(MIGRATIONS) + 1))


def test_rendered_sql_sets_a_lock_timeout_per_version() -> None:
    text = render(TableNames())
    assert text.count("SET LOCAL lock_timeout = '30000ms';") == len(MIGRATIONS)
    assert text.index("BEGIN;") < text.index("SET LOCAL lock_timeout") < text.index('CREATE TABLE "public"."af_history')


@pytest.mark.parametrize(
    ("lock_timeout", "rendered"),
    [
        (timedelta(milliseconds=1001), "'1001ms'"),
        (timedelta(milliseconds=1), "'1ms'"),
        (timedelta(seconds=30), "'30000ms'"),
    ],
)
def test_lock_timeout_renders_exact_milliseconds(lock_timeout: timedelta, rendered: str) -> None:
    from agent_framework_community_postgres._migrations import _lock_timeout_literal

    assert _lock_timeout_literal(lock_timeout).as_string() == rendered


@pytest.mark.parametrize("lock_timeout", [timedelta(0), timedelta(microseconds=999), timedelta(seconds=-1)])
async def test_migrate_refuses_a_lock_timeout_postgres_would_read_as_none(lock_timeout: timedelta) -> None:
    handle = cast(ClientHandle, object())  # never reached: the check runs before any connection
    with pytest.raises(ValueError, match="at least one millisecond"):
        await migrate(handle, TableNames(), lock_timeout=lock_timeout)


def test_longest_identifier_matches_the_migrations() -> None:
    from agent_framework_community_postgres._client import _LONGEST_IDENTIFIER

    text = render(TableNames(schema="public", prefix=""))
    names = {a or b for a, b in re.findall(r'"public"\."([a-z_]+)"|CREATE INDEX "([a-z_]+)"', text)}
    assert max(names, key=len) == _LONGEST_IDENTIFIER


def test_v1_creates_the_final_checkpoint_shape() -> None:
    text = render(TableNames())
    assert "tenant_id text NOT NULL DEFAULT ''" in text
    assert "payload_format text NOT NULL DEFAULT 'agent-framework.python.checkpoint.v1'" in text
    assert "sequence bigint GENERATED ALWAYS AS IDENTITY" in text
    assert "iteration_count" not in text and "revision" not in text and "documents" not in text
    assert text.count("expires_idx") == 5  # history, sessions, checkpoints, thread_snapshots, leases
