import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/matias-io/community-agent-framework-postgres/"


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
        "azure-entra",
    ):
        assert (ROOT / "docs" / f"{page}.md").exists(), page
        assert re.search(rf"docs/{page}\.md", readme), page


def test_changelog_has_the_first_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in changelog or "## [0.1.0]" in changelog


def _markdown_links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", text)


def test_readme_has_no_relative_links() -> None:
    # PyPI renders the README without the repository, so a relative link there is a dead link.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    relative = [link for link in _markdown_links(readme) if not link.startswith(("https://", "http://", "#"))]
    assert relative == []


def test_readme_links_into_the_repository_name_this_release_and_existing_files() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    links = [link for link in _markdown_links(readme) if link.startswith(REPOSITORY)]
    assert links
    for link in links:
        kind, ref, path = link.removeprefix(REPOSITORY).split("/", 2)
        assert kind in ("blob", "tree"), link
        assert ref == f"v{version}", link
        assert (ROOT / path.split("#")[0]).exists(), link


def test_readme_anchors_name_headings() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    slugs = {
        re.sub(r"[^a-z0-9 -]", "", heading.strip().lower()).replace(" ", "-")
        for heading in re.findall(r"^#+ (.+)$", readme, flags=re.MULTILINE)
    }
    for link in _markdown_links(readme):
        if link.startswith("#"):
            assert link[1:] in slugs, link
