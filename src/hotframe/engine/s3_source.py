"""
S3 module source — download, verify, cache module packages from S3.

Handles:
- Parallel downloads via ``asyncio.gather``
- ETag-based cache validation (skip re-download if unchanged)
- SHA256 integrity verification
- tar.gz / ZIP extraction to local cache directory

S3 object key convention:
    ``cloud/modules/{module_id}/v{version}.zip``
    Built from module_id + version — never stored in DB.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tarfile
from io import BytesIO
from pathlib import Path

try:
    import aioboto3  # type: ignore[import-not-found]
except ImportError:
    aioboto3 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Fixed S3 prefix — all module ZIPs live under this path
S3_MODULES_PREFIX = "cloud/modules"


def build_module_object_key(module_id: str, version: str) -> str:
    """Build the S3 object key for a module ZIP.

    Convention: ``cloud/modules/{module_id}/v{version}.zip``
    """
    return f"{S3_MODULES_PREFIX}/{module_id}/v{version}.zip"


class IntegrityError(Exception):
    """Raised when SHA256 verification fails."""


class S3ModuleSource:
    """
    Downloads module packages from S3 with caching and integrity verification.

    The local cache at ``cache_dir`` is ephemeral (``/tmp/modules/`` on ECS).
    ETag values are stored in memory and on disk so that warm containers
    skip re-downloads when the S3 object hasn't changed.
    """

    def __init__(
        self,
        bucket: str,
        cache_dir: Path,
        region: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.cache_dir = cache_dir
        if region is None:
            from hotframe.config.settings import get_settings

            region = get_settings().AWS_REGION
        self.region = region
        self._etag_cache: dict[str, str] = {}
        if aioboto3 is None:
            raise ImportError(
                "aioboto3 is required for S3ModuleSource. Install it with: pip install aioboto3"
            )
        self._session = aioboto3.Session()

        # Ensure cache dir exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download(
        self,
        module_id: str,
        version: str,
        expected_sha256: str = "",
    ) -> Path:
        """
        Download a module from S3, verify SHA256, extract.

        S3 key is calculated from module_id + version (never from DB).

        Returns the local path to the extracted module directory.

        Cache strategy:
            1. If local dir exists AND stored ETag matches current → return cached
            2. Otherwise download, verify, extract, store ETag
        """
        object_key = build_module_object_key(module_id, version)
        local_path = self.cache_dir / module_id

        # Check local cache
        if local_path.exists():
            cached_etag = self._etag_cache.get(module_id)
            if cached_etag is not None:
                current_etag = await self._get_object_etag(object_key)
                if current_etag and cached_etag == current_etag:
                    logger.debug(
                        "Cache hit for %s v%s (ETag match)",
                        module_id,
                        version,
                    )
                    return local_path

        # Download
        logger.info(
            "Downloading %s v%s from s3://%s/%s", module_id, version, self.bucket, object_key
        )
        data = await self._download_object(object_key)

        # Verify SHA256
        self._verify_sha256(data, expected_sha256, module_id)

        # Extract
        self._extract(data, local_path)

        # Store ETag
        etag = await self._get_object_etag(object_key)
        if etag:
            self._etag_cache[module_id] = etag
            self._store_etag_file(module_id, etag)

        logger.info(
            "Downloaded and extracted %s v%s (%d bytes)",
            module_id,
            version,
            len(data),
        )
        return local_path

    async def download_many(
        self,
        modules: list[tuple[str, str, str]],
    ) -> dict[str, Path]:
        """
        Parallel download of multiple modules.

        Each tuple is ``(module_id, version, expected_sha256)``.
        Returns a dict mapping ``module_id`` to the local extracted path.
        Failed downloads are logged and excluded from the result.
        """
        results: dict[str, Path] = {}

        async def _download_one(
            module_id: str,
            version: str,
            sha256: str,
        ) -> tuple[str, Path | None]:
            try:
                path = await self.download(module_id, version, sha256)
                return module_id, path
            except Exception:
                logger.exception("Failed to download %s v%s", module_id, version)
                return module_id, None

        tasks = [_download_one(mid, ver, sha) for mid, ver, sha in modules]
        completed = await asyncio.gather(*tasks)

        for module_id, path in completed:
            if path is not None:
                results[module_id] = path

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_object_etag(self, object_key: str) -> str | None:
        """Get the ETag for an S3 object (HEAD request)."""
        try:
            async with self._session.client(
                "s3",
                region_name=self.region,
            ) as s3:
                response = await s3.head_object(Bucket=self.bucket, Key=object_key)
                return response.get("ETag", "").strip('"')
        except Exception:
            logger.debug("Could not get ETag for %s", object_key)
            return None

    async def _download_object(self, object_key: str) -> bytes:
        """Download the raw bytes of an S3 object with retry and exponential backoff."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with self._session.client(
                    "s3",
                    region_name=self.region,
                ) as s3:
                    response = await s3.get_object(Bucket=self.bucket, Key=object_key)
                    return await response["Body"].read()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                delay = 2**attempt  # 1, 2, 4 seconds
                logger.warning(
                    "S3 download failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1,
                    max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
        # Unreachable, but satisfies type checker
        raise RuntimeError("S3 download failed after all retries")

    @staticmethod
    def _verify_sha256(data: bytes, expected: str, module_id: str) -> None:
        """Verify SHA256 integrity. Raises IntegrityError on mismatch."""
        if not expected:
            logger.warning("No SHA256 checksum for %s — skipping verification", module_id)
            return
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise IntegrityError(
                f"SHA256 mismatch for {module_id}: expected {expected}, got {actual}"
            )

    @staticmethod
    def _extract(data: bytes, target: Path) -> None:
        """Extract a tar.gz or ZIP archive to the target directory."""
        import zipfile

        # Remove existing dir to ensure clean extraction
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        buf = BytesIO(data)

        # Detect format: ZIP or tar.gz
        if zipfile.is_zipfile(buf):
            buf.seek(0)
            with zipfile.ZipFile(buf) as zf:
                # Detect common prefix (e.g. "assistant/" wrapping all files)
                names = [n for n in zf.namelist() if not n.endswith("/")]
                prefix = ""
                if names:
                    parts = names[0].split("/")
                    if len(parts) > 1:
                        candidate = parts[0] + "/"
                        if all(n.startswith(candidate) for n in names):
                            prefix = candidate

                for info in zf.infolist():
                    # Security: filter out absolute paths and path traversal
                    if info.filename.startswith("/") or ".." in info.filename:
                        logger.warning("Skipping unsafe zip member: %s", info.filename)
                        continue
                    # Strip common prefix to flatten (e.g. assistant/module.py → module.py)
                    if prefix and info.filename.startswith(prefix):
                        info.filename = info.filename[len(prefix) :]
                        if not info.filename:
                            continue
                    zf.extract(info, path=target)
        else:
            buf.seek(0)
            with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                # Security: filter out absolute paths and path traversal
                safe_members = []
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        logger.warning(
                            "Skipping unsafe tar member: %s",
                            member.name,
                        )
                        continue
                    safe_members.append(member)

                tar.extractall(path=target, members=safe_members)

    def _store_etag_file(self, module_id: str, etag: str) -> None:
        """Persist ETag to disk for cross-restart cache validation."""
        etag_file = self.cache_dir / f".{module_id}.etag"
        try:
            etag_file.write_text(etag, encoding="utf-8")
        except OSError:
            pass

    def load_cached_etags(self) -> None:
        """Load ETag files from disk on startup."""
        for etag_file in self.cache_dir.glob(".*.etag"):
            module_id = etag_file.stem.lstrip(".")
            try:
                self._etag_cache[module_id] = etag_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass

    def clear_cache(self, module_id: str | None = None) -> None:
        """Remove cached module files. If module_id is None, clear all."""
        if module_id:
            path = self.cache_dir / module_id
            if path.exists():
                shutil.rmtree(path)
            self._etag_cache.pop(module_id, None)
            etag_file = self.cache_dir / f".{module_id}.etag"
            etag_file.unlink(missing_ok=True)
        else:
            for item in self.cache_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                elif item.name.endswith(".etag"):
                    item.unlink(missing_ok=True)
            self._etag_cache.clear()
