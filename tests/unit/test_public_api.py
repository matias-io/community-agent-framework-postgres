import agent_framework_community_postgres as package


def test_public_names_are_exported() -> None:
    expected = {
        "PostgresPersistence",
        "PostgresClient",
        "RetentionMode",
        "PostgresHistoryProvider",
        "PostgresSessionStore",
        "PostgresCheckpointStorage",
        "PostgresAGUIThreadSnapshotStore",
        "PostgresDocumentStore",
        "Document",
        "DocumentSummary",
        "PostgresLeases",
        "Lease",
        "RetentionPolicy",
        "PurgeReport",
        "MigrationReport",
        "PostgresSettings",
        "TableNames",
        "PostgresStorageError",
        "RevisionConflict",
        "LeaseUnavailable",
        "LeaseLost",
        "MIGRATIONS",
        "render_migrations",
        "__version__",
    }
    assert expected <= set(package.__all__)
    assert package.PostgresAGUIThreadSnapshotStore.__name__ == "PostgresAGUIThreadSnapshotStore"
    assert "PostgresAGUIThreadSnapshotStore" in dir(package)


def test_unknown_attribute_raises() -> None:
    try:
        package.Nope  # noqa: B018
    except AttributeError as exc:
        assert "Nope" in str(exc)
    else:
        raise AssertionError("expected AttributeError")
