import re

from agent_framework_community_postgres._client import TableNames
from agent_framework_community_postgres._migrations import MIGRATIONS, render


def test_render_lists_every_table_with_quoted_names() -> None:
    text = render(TableNames(schema="agents", prefix="af_"))
    for table in (
        "migrations",
        "history_messages",
        "sessions",
        "checkpoints",
        "thread_snapshots",
        "documents",
        "leases",
    ):
        assert f'"agents"."af_{table}"' in text
    assert "-- version 1" in text
    assert text.count("CREATE TABLE") == 7


def test_render_can_select_versions() -> None:
    assert render(TableNames(), versions=[]).strip() == ""
    assert len(MIGRATIONS) == 1


def test_every_identifier_fits_at_the_longest_allowed_prefix() -> None:
    # TableNames accepts a prefix up to the length that keeps its longest known identifier within 63 bytes.
    # If a migration adds a longer table or index name, this fails until _client._LONGEST_IDENTIFIER is updated.
    longest_ok = "p" * 35
    text = render(TableNames(prefix=longest_ok))
    for name in re.findall(r'"([^"]+)"', text):
        assert len(name.encode("utf-8")) <= 63, name
