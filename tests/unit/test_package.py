from importlib.metadata import version

import agent_framework_community_postgres as package


def test_version_is_exposed() -> None:
    assert package.__version__ == version("community-agent-framework-postgres")
    assert package.__version__ != "0.0.0"  # the fallback for a source tree that was never installed
    assert "__version__" in package.__all__
