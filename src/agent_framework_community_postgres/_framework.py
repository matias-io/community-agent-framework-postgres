"""Every import of a private Agent Framework name, and the check against the tested MAF versions, in one place.

Microsoft's own Cosmos DB and Redis packages import these same names from the
same private modules. Keeping them here means an upstream rename breaks one
file, and ``tests/unit/test_framework.py`` fails before a user notices.

Verified against agent-framework-core 1.20.0 on 4 October 2026. 1.20 is the
floor because 1.19's ``filter_new_messages`` dropped any incoming message whose
content matched one already stored, so a repeated "yes" was lost.
"""

import logging
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

SUPPORTED_CORE = ">=1.20.0,<2"
"""The declared ``agent-framework-core`` range: MAF keeps breaking changes for major versions."""

TESTED_CORE = ("1.20",)
"""The ``agent-framework-core`` minors this release was tested on (major.minor)."""

TESTED_AG_UI = ("1.5",)
"""The ``agent-framework-ag-ui`` minors this release was tested on (major.minor)."""

logger = logging.getLogger("agent_framework_community_postgres")
_logged = False


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


@dataclass(frozen=True)
class Compatibility:
    """Installed Agent Framework versions next to the minors this release was tested on."""

    core_installed: str
    core_tested: tuple[str, ...]
    ag_ui_installed: str | None
    ag_ui_tested: tuple[str, ...]

    def _pairs(self) -> list[tuple[str, str, tuple[str, ...]]]:
        pairs = [("agent-framework-core", self.core_installed, self.core_tested)]
        if self.ag_ui_installed is not None:
            pairs.append(("agent-framework-ag-ui", self.ag_ui_installed, self.ag_ui_tested))
        return pairs

    @property
    def untested(self) -> tuple[str, ...]:
        """Distributions whose installed major.minor is newer than any tested one."""
        return tuple(name for name, installed, tested in self._pairs() if _newer(installed, tested) is not None)

    @property
    def out_of_range(self) -> tuple[str, ...]:
        """Distributions whose installed major is newer than the tested major: outside the declared range."""
        return tuple(name for name, installed, tested in self._pairs() if _newer(installed, tested) == "major")


def _newer(installed: str, tested: tuple[str, ...]) -> str | None:
    current = _major_minor(installed)
    newest = max(filter(None, map(_major_minor, tested)))
    if current is None or current <= newest:
        return None
    return "major" if current[0] > newest[0] else "minor"


def compatibility() -> Compatibility:
    """What is installed and what was tested; ``untested`` is empty when every version is covered."""
    try:
        ag_ui: str | None = version("agent-framework-ag-ui")
    except PackageNotFoundError:
        ag_ui = None
    return Compatibility(installed_core_version(), TESTED_CORE, ag_ui, TESTED_AG_UI)


def log_untested_once() -> None:
    """Log one warning per process when an installed Agent Framework is newer than any tested minor.

    Called by every store and hub constructor. Importing the package logs nothing.
    """
    global _logged
    if _logged:
        return
    _logged = True
    report = compatibility()
    for name, installed, tested in report._pairs():  # pyright: ignore[reportPrivateUsage]
        kind = _newer(installed, tested)
        if kind is None:
            continue
        current = _major_minor(installed)
        if current is None:
            continue
        if kind == "major":
            advice = (
                f"{name} {installed} is a new major version, outside the declared range and likely to break;"
                f" pin {name}<{current[0]} and open an issue."
            )
        else:
            advice = (
                "It is expected to work, since Agent Framework keeps breaking changes for major versions;"
                f" if something fails, pin {name}<{current[0]}.{current[1]} and open an issue."
            )
        logger.warning(
            "community-agent-framework-postgres has not been tested with %s %s (tested: %s). %s",
            name,
            installed,
            ", ".join(tested),
            advice,
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
    "Compatibility",
    "compatibility",
    "decode_checkpoint_value",
    "encode_checkpoint_value",
    "filter_new_messages",
    "installed_core_version",
    "log_untested_once",
]
