"""Every import of a private Agent Framework name, in one place.

Microsoft's own Cosmos DB and Redis packages import these same names from the
same private modules. Keeping them here means an upstream rename breaks one
file, and ``tests/unit/test_framework.py`` fails before a user notices.

Verified against agent-framework-core 1.19.0 and 1.20.0 on 2 October 2026.
Their signatures did not change, but 1.20 changed ``filter_new_messages``: when
the stored history is one user message without an id, an incoming batch that
starts with that same message is kept as a repeated input instead of dropped as
a replay. ``PostgresHistoryProvider`` follows whichever rule is installed.
"""

from importlib.metadata import PackageNotFoundError, version

SUPPORTED_CORE = ">=1.19.0,<1.21"


def installed_core_version() -> str:
    """The installed agent-framework-core version, or ``unknown``."""
    try:
        return version("agent-framework-core")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


try:
    # The shared dedupe every history provider (Cosmos, Redis, File) uses.
    from agent_framework._sessions import filter_new_messages

    # The hybrid JSON + restricted-pickle encoding File and Cosmos storage use.
    from agent_framework._workflows._checkpoint_encoding import decode_checkpoint_value, encode_checkpoint_value
except ImportError as exc:  # pragma: no cover - exercised only by an incompatible upstream release
    raise ImportError(
        f"agent-framework-core {installed_core_version()} no longer provides a private name this package relies on "
        f"({exc}). This release supports agent-framework-core {SUPPORTED_CORE}; check for a newer "
        "community-agent-framework-postgres."
    ) from exc

__all__ = [
    "SUPPORTED_CORE",
    "decode_checkpoint_value",
    "encode_checkpoint_value",
    "filter_new_messages",
    "installed_core_version",
]
