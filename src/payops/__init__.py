"""Payment operations agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("payops")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0"
