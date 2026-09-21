"""PostgreSQL storage for Microsoft Agent Framework."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("community-agent-framework-postgres")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
