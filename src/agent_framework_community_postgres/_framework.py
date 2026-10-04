"""Every import of a private Agent Framework name, and the check of which MAF versions were tested, in one place.

Microsoft's own Cosmos DB and Redis packages import these same names from the
same private modules. Keeping them here means an upstream rename breaks one
file, and ``tests/unit/test_framework.py`` fails before a user notices.

Verified against agent-framework-core 1.19.0 and 1.20.0 on 2 October 2026.
Their signatures did not change, but 1.20 changed ``filter_new_messages``: when
the stored history is one user message without an id, an incoming batch that
starts with that same message is kept as a repeated input instead of dropped as
a replay. ``PostgresHistoryProvider`` follows whichever rule is installed.
"""

import re
import warnings
from importlib.metadata import PackageNotFoundError, version

SUPPORTED_CORE = ">=1.19.0,<2"
"""The declared ``agent-framework-core`` range: MAF keeps breaking changes for major versions."""

TESTED_CORE = ("1.19", "1.20")
"""The ``agent-framework-core`` minors this release was tested on (major.minor)."""

TESTED_AG_UI = ("1.4", "1.5")
"""The ``agent-framework-ag-ui`` minors this release was tested on (major.minor)."""


class UntestedAgentFrameworkWarning(UserWarning):
    """The installed Agent Framework is newer than any version this release was tested on.

    It is expected to work, because Agent Framework keeps breaking changes for major versions.

    The warning is emitted while the package is imported, so a filter must match the message prefix
    ``community-agent-framework-postgres has not been tested`` and be installed before the import. For example,
    ``warnings.filterwarnings("ignore", message="community-agent-framework-postgres has not been tested")``,
    ``PYTHONWARNINGS=ignore:community-agent-framework-postgres has not been tested``, or a pytest ``filterwarnings``
    entry ``ignore:community-agent-framework-postgres has not been tested``. Filtering by this class works only for
    checks made after the import.
    """


def installed_core_version() -> str:
    """The installed agent-framework-core version, or ``unknown``."""
    try:
        return version("agent-framework-core")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _major_minor(text: str) -> tuple[int, int] | None:
    """The numeric major.minor at the start of a version string; pre-release and local parts are ignored."""
    match = re.match(r"(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2])) if match else None


def warn_if_untested_agent_framework(stacklevel: int = 2) -> None:
    """Warn once per package newer than the newest tested minor; never for older or tested versions."""
    for distribution, tested in (("agent-framework-core", TESTED_CORE), ("agent-framework-ag-ui", TESTED_AG_UI)):
        try:
            installed = version(distribution)
        except PackageNotFoundError:
            continue
        current = _major_minor(installed)
        newest = max(filter(None, map(_major_minor, tested)))
        if current is None or current <= newest:
            continue
        warnings.warn(
            UntestedAgentFrameworkWarning(
                f"community-agent-framework-postgres has not been tested with {distribution} {installed}"
                f" (tested: {', '.join(tested)}). It is expected to work, since Agent Framework keeps breaking"
                f" changes for major versions; if something fails, pin {distribution}<{current[0]}.{current[1]} and"
                " open an issue. To silence this warning, filter its message before the first import:"
                " warnings.filterwarnings('ignore', message='community-agent-framework-postgres has not been tested')."
            ),
            stacklevel=stacklevel + 1,
        )


try:
    # The shared dedupe every history provider (Cosmos, Redis, File) uses.
    from agent_framework._sessions import filter_new_messages

    # The hybrid JSON + restricted-pickle encoding File and Cosmos storage use.
    from agent_framework._workflows._checkpoint_encoding import decode_checkpoint_value, encode_checkpoint_value
except ImportError as exc:  # pragma: no cover - exercised only by an incompatible upstream release
    raise ImportError(
        f"agent-framework-core {installed_core_version()} no longer provides a private name this package relies on "
        f"({exc}). This release declares agent-framework-core {SUPPORTED_CORE} and was tested on "
        f"{', '.join(TESTED_CORE)}; pin an earlier agent-framework-core or check for a newer "
        "community-agent-framework-postgres, and open an issue."
    ) from exc

__all__ = [
    "SUPPORTED_CORE",
    "TESTED_AG_UI",
    "TESTED_CORE",
    "UntestedAgentFrameworkWarning",
    "decode_checkpoint_value",
    "encode_checkpoint_value",
    "filter_new_messages",
    "installed_core_version",
    "warn_if_untested_agent_framework",
]
