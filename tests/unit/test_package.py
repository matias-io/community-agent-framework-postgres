import agent_framework_community_postgres as package


def test_version_is_exposed() -> None:
    assert package.__version__
    assert "__version__" in package.__all__
