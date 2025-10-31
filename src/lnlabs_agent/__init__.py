from __future__ import annotations

import os

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
except ImportError:  # pragma: no cover
    from importlib_metadata import PackageNotFoundError, version as _pkg_version  # type: ignore

__all__ = ["__version__"]


def _detect_version() -> str:
    env_version = os.getenv("LNLABS_AGENT_VERSION")
    if env_version:
        return env_version
    try:
        return _pkg_version("lnlabs-agent")
    except PackageNotFoundError:
        return "dev"


__version__ = _detect_version()
