# SPDX-License-Identifier: Apache-2.0
"""
Marketplace HTTP client — resolves and downloads modules from any marketplace.

The marketplace URL is configured via ``settings.MODULE_MARKETPLACE_URL``.
Any server that implements the resolve endpoint can serve as a marketplace.

Protocol::

    GET {base_url}/{module_id}/resolve/
    GET {base_url}/{module_id}/resolve/?version=2.4.7

    Response:
    {
        "module_id": "sales",
        "version": "2.4.7",
        "download_url": "https://cdn.example.com/modules/sales/v2.4.7.zip",
        "checksum_sha256": "abc123...",
        "dependencies": ["customers>=2.0.0", "inventory"],
        "size_bytes": 204800
    }
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ModuleDownloadInfo:
    """Resolved module metadata from the marketplace."""

    module_id: str
    version: str
    download_url: str
    checksum_sha256: str = ""
    dependencies: list[str] = field(default_factory=list)
    size_bytes: int = 0


class MarketplaceClient:
    """HTTP client for module marketplace API.

    Resolves module metadata and downloads zips from any HTTP(S) source.

    Usage::

        client = MarketplaceClient("https://marketplace.example.com/api/v1/modules")
        info = await client.resolve("sales")
        path = await client.download(info.download_url, dest_dir, info.checksum_sha256)
    """

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def resolve(
        self,
        module_id: str,
        version: str | None = None,
    ) -> ModuleDownloadInfo:
        """Resolve a module from the marketplace.

        Args:
            module_id: Module identifier (e.g. "sales").
            version: Specific version (e.g. "2.4.7"). None = latest.

        Returns:
            ModuleDownloadInfo with download URL, checksum, dependencies.

        Raises:
            MarketplaceError: If the module is not found or the server fails.
        """
        url = f"{self.base_url}/{module_id}/resolve/"
        params = {}
        if version:
            params["version"] = version

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url, params=params)
            except httpx.RequestError as exc:
                raise MarketplaceError(f"Failed to connect to marketplace: {exc}") from exc

            if response.status_code == 404:
                raise MarketplaceError(f"Module '{module_id}' not found in marketplace")
            if response.status_code != 200:
                raise MarketplaceError(
                    f"Marketplace error {response.status_code}: {response.text[:200]}"
                )

            data = response.json()

        return ModuleDownloadInfo(
            module_id=data.get("module_id", module_id),
            version=data.get("version", ""),
            download_url=data.get("download_url", ""),
            checksum_sha256=data.get("checksum_sha256", ""),
            dependencies=data.get("dependencies", []),
            size_bytes=data.get("size_bytes", 0),
        )

    async def download(
        self,
        download_url: str,
        dest_dir: Path,
        checksum: str = "",
    ) -> Path:
        """Download a zip from URL and extract to dest_dir.

        Args:
            download_url: Full URL to the zip file.
            dest_dir: Directory to extract into (e.g. modules/).
            checksum: Expected SHA256 checksum (optional).

        Returns:
            Path to the extracted module directory.

        Raises:
            MarketplaceError: On download failure, checksum mismatch, or bad zip.
        """
        logger.info("Downloading module from %s", download_url)

        # Download to temp file
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    response = await client.get(download_url)
                except httpx.RequestError as exc:
                    raise MarketplaceError(f"Download failed: {exc}") from exc

                if response.status_code != 200:
                    raise MarketplaceError(f"Download failed: HTTP {response.status_code}")

                tmp_path.write_bytes(response.content)

            # Verify checksum
            if checksum:
                actual = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
                if actual != checksum:
                    raise MarketplaceError(
                        f"Checksum mismatch: expected {checksum[:16]}..., got {actual[:16]}..."
                    )

            # Extract
            return self._extract_zip(tmp_path, dest_dir)

        finally:
            tmp_path.unlink(missing_ok=True)

    async def resolve_all_dependencies(
        self,
        module_id: str,
        version: str | None = None,
        *,
        already_installed: set[str] | None = None,
    ) -> list[ModuleDownloadInfo]:
        """Recursively resolve all dependencies in topological order.

        Returns a list where dependencies come first, the requested module
        is last. Modules in ``already_installed`` are skipped.

        Args:
            module_id: The module to resolve.
            version: Specific version (None = latest).
            already_installed: Set of module_ids already installed (skip these).

        Returns:
            List of ModuleDownloadInfo in install order (deps first).
        """
        installed = already_installed or set()
        resolved: list[ModuleDownloadInfo] = []
        visited: set[str] = set()
        queue: deque[tuple[str, str | None]] = deque([(module_id, version)])

        # BFS to collect all dependencies
        all_modules: dict[str, ModuleDownloadInfo] = {}

        while queue:
            mid, ver = queue.popleft()
            if mid in visited or mid in installed:
                continue
            visited.add(mid)

            try:
                info = await self.resolve(mid, ver)
            except MarketplaceError:
                logger.warning("Could not resolve dependency: %s", mid)
                continue

            all_modules[mid] = info

            for dep_spec in info.dependencies:
                dep_id = (
                    dep_spec.split(">=")[0]
                    .split("==")[0]
                    .split("<=")[0]
                    .split(">")[0]
                    .split("<")[0]
                    .strip()
                )
                if dep_id not in visited and dep_id not in installed:
                    queue.append((dep_id, None))

        # Topological sort (dependencies first)
        # Build adjacency: module → its dependencies
        order: list[str] = []
        remaining = set(all_modules.keys())

        while remaining:
            # Find modules with no unresolved dependencies
            ready = []
            for mid in remaining:
                deps = [
                    d.split(">=")[0].split("==")[0].strip() for d in all_modules[mid].dependencies
                ]
                unresolved = [d for d in deps if d in remaining and d != mid]
                if not unresolved:
                    ready.append(mid)

            if not ready:
                # Cycle — just add remaining in any order
                logger.warning("Dependency cycle detected among: %s", remaining)
                order.extend(remaining)
                break

            for mid in sorted(ready):
                order.append(mid)
                remaining.discard(mid)

        resolved = [all_modules[mid] for mid in order if mid in all_modules]
        return resolved

    @staticmethod
    def _extract_zip(zip_path: Path, dest_dir: Path) -> Path:
        """Extract a zip file into dest_dir. Returns path to the module directory."""
        if not zipfile.is_zipfile(zip_path):
            raise MarketplaceError(f"Not a valid zip file: {zip_path}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    member_path = Path(member)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise MarketplaceError(f"Zip contains unsafe path: {member}")
                zf.extractall(tmp)

            # Find module.py — at root or one level deep
            module_root = None
            if (tmp / "module.py").exists():
                module_root = tmp
            else:
                for child in tmp.iterdir():
                    if child.is_dir() and (child / "module.py").exists():
                        module_root = child
                        break

            if module_root is None:
                raise MarketplaceError("Zip does not contain module.py")

            # Derive name, strip version suffix
            name = module_root.name
            if "-" in name:
                name = name.rsplit("-", 1)[0]

            # Copy to dest
            import shutil

            target = dest_dir / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(module_root, target)

            logger.info("Extracted module %s to %s", name, target)
            return target


class MarketplaceError(Exception):
    """Error from marketplace operations."""

    pass
