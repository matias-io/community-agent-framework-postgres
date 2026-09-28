import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_names_every_public_store() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "PostgresPersistence",
        "PostgresHistoryProvider",
        "PostgresSessionStore",
        "PostgresCheckpointStorage",
        "PostgresAGUIThreadSnapshotStore",
        "PostgresDocumentStore",
        "RetentionPolicy",
    ):
        assert name in readme, name
    assert "agent-framework-core" in readme and "1.19" in readme


def test_docs_pages_exist_and_are_linked_from_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for page in (
        "history",
        "sessions",
        "checkpoints",
        "thread-snapshots",
        "documents-and-leases",
        "retention",
        "migrations",
        "compatibility",
    ):
        assert (ROOT / "docs" / f"{page}.md").exists(), page
        assert re.search(rf"docs/{page}\.md", readme), page


def test_changelog_has_the_first_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in changelog or "## [0.1.0]" in changelog
