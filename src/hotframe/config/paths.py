"""
Data paths — ephemeral and configurable.

Hotframe is fully stateless: DB + S3 are the sources of truth.
Local filesystem paths are ephemeral caches only.

- Media: S3 in production, /tmp/hotframe-media/ in dev (via MediaService)
- Module cache: /tmp/modules/ (rebuilt from S3 on cold start)
- Temp files: /tmp/hotframe-temp/ (ephemeral)

No persistent /app/data/ directory — the container filesystem is 100% ephemeral.
"""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path


class DataPaths:
    """
    Manages ephemeral data directories for the hotframe instance.

    All paths are under /tmp/ — they are NOT persistent.
    In production, persistent data lives in S3 (media, reports, backups).
    """

    def __init__(self, base: Path | None = None) -> None:
        if base is not None:
            self._base = base.resolve()
        elif env := os.environ.get("DATA_PATH"):
            self._base = Path(env).resolve()
        else:
            # Ephemeral base — all local data is cache/temp
            self._base = Path("/tmp/hotframe-data")

    @cached_property
    def base(self) -> Path:
        return self._base

    @cached_property
    def media(self) -> Path:
        """Local media cache (dev only). Production uses S3 via MediaService."""
        return Path("/tmp/hotframe-media")

    @cached_property
    def modules(self) -> Path:
        """Module code cache (rebuilt from S3 on cold start)."""
        return Path("/tmp/modules")

    @cached_property
    def reports(self) -> Path:
        """Ephemeral report generation dir. Final reports go to S3."""
        return self._base / "reports"

    @cached_property
    def temp(self) -> Path:
        """General temp directory."""
        return self._base / "temp"

    @cached_property
    def cache(self) -> Path:
        """General cache directory."""
        return self._base / "cache"

    @property
    def all_dirs(self) -> list[Path]:
        return [
            self.base,
            self.media,
            self.modules,
            self.reports,
            self.temp,
            self.cache,
        ]

    def ensure_dirs(self) -> None:
        """Create all data directories if they don't exist."""
        for d in self.all_dirs:
            d.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"DataPaths(base={self._base})"


_data_paths: DataPaths | None = None


def get_data_paths() -> DataPaths:
    """Return cached singleton DataPaths instance."""
    global _data_paths
    if _data_paths is None:
        _data_paths = DataPaths()
    return _data_paths


def reset_data_paths() -> None:
    """Reset cached data paths (for testing)."""
    global _data_paths
    _data_paths = None
