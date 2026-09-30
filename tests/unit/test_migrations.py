import re

import pytest

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
