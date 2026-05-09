"""
Module manifest — Pydantic strict validation of module.py contents.

Every module must have a ``module.py`` at its root with specific attributes.
This module defines the schema (ModuleManifest) and the loader that extracts
those attributes into a validated Pydantic model.

If validation fails, the module CANNOT load and its status becomes ``error``.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Sub-models
# ------------------------------------------------------------------


class MenuConfig(BaseModel):
    """Module sidebar menu entry configuration."""

    label: str
    icon: str = "cube-outline"
    order: int = 50


class NavigationItem(BaseModel):
    """A single tab/section inside a module's navigation bar."""

    label: str
    icon: str
    id: str
    view: str = ""


# ------------------------------------------------------------------
# ModuleManifest
# ------------------------------------------------------------------


class ModuleManifest(BaseModel):
    """
    Strict schema for ``module.py`` attributes.

    If any required field is missing or fails validation the module
    is rejected and its DB status set to ``error``.
    """

    MODULE_ID: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    MODULE_NAME: str
    MODULE_VERSION: str = Field(pattern=r"^\d+\.\d+\.\d+")
    MODULE_ICON: str = "cube-outline"
    MODULE_DESCRIPTION: str = ""
    MODULE_AUTHOR: str = ""
    HAS_MODELS: bool = False

    MENU: MenuConfig | None = None
    NAVIGATION: list[NavigationItem] = []
    PERMISSIONS: list[str] = []
    ROLE_PERMISSIONS: dict[str, list[str | tuple]] = {}
    DEPENDENCIES: list[str] = []

    @field_validator("PERMISSIONS", mode="before")
    @classmethod
    def normalize_permissions(cls, v: Any) -> list[str]:
        """Accept both 'codename' strings and ('codename', 'description') tuples."""
        result = []
        for item in v:
            if isinstance(item, (tuple, list)):
                result.append(str(item[0]))
            else:
                result.append(str(item))
        return result

    MIDDLEWARE: str | None = None
    SCHEDULED_TASKS: list[dict] = []
    PRICING: dict | None = None


# ------------------------------------------------------------------
# Manifest attributes — the exhaustive list we read from module.py
# ------------------------------------------------------------------

_MANIFEST_FIELDS: set[str] = set(ModuleManifest.model_fields.keys())


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------


def load_manifest(module_path: Path) -> ModuleManifest:
    """
    Import ``module.py`` from *module_path* and build a validated manifest.

    The file is loaded via ``importlib`` into an isolated spec so it does not
    pollute ``sys.modules`` with a permanent entry. Attributes listed in
    :data:`_MANIFEST_FIELDS` are extracted and passed to :class:`ModuleManifest`.

    Args:
        module_path: Directory containing ``module.py``.

    Returns:
        A validated :class:`ModuleManifest` instance.

    Raises:
        FileNotFoundError: If ``module.py`` does not exist.
        pydantic.ValidationError: If the extracted attributes fail validation.
    """
    module_py = module_path / "module.py"
    if not module_py.exists():
        raise FileNotFoundError(f"module.py not found in {module_path}")

    # Use a temporary module name to avoid collisions
    tmp_name = f"_manifest_loader_{module_path.name}"

    spec = importlib.util.spec_from_file_location(tmp_name, str(module_py))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {module_py}")

    mod = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(mod)
    finally:
        # Clean up the temporary module — never keep it around
        sys.modules.pop(tmp_name, None)

    # Extract manifest attributes
    data: dict[str, Any] = {}
    for attr in _MANIFEST_FIELDS:
        value = getattr(mod, attr, None)
        if value is not None:
            # MenuConfig may be provided as a plain dict
            data[attr] = value

    return ModuleManifest(**data)


def manifest_to_dict(manifest: ModuleManifest) -> dict[str, Any]:
    """Serialize a manifest to a JSON-safe dict for storing in hub_module.manifest.

    Produces short keys (``name``, ``icon``, ``dependencies``, …) instead of
    the raw Pydantic field names (``MODULE_NAME``, ``MODULE_ICON``, …) so that
    templates, routes, and APIs can access them consistently.
    """
    raw = manifest.model_dump(mode="json")
    _KEY_MAP: dict[str, str] = {
        "MODULE_ID": "module_id",
        "MODULE_NAME": "name",
        "MODULE_VERSION": "version",
        "MODULE_ICON": "icon",
        "MODULE_DESCRIPTION": "description",
        "MODULE_AUTHOR": "author",
        "HAS_MODELS": "has_models",
        "MENU": "menu",
        "NAVIGATION": "navigation",
        "PERMISSIONS": "permissions",
        "ROLE_PERMISSIONS": "role_permissions",
        "DEPENDENCIES": "dependencies",
        "MIDDLEWARE": "middleware",
        "SCHEDULED_TASKS": "scheduled_tasks",
        "PRICING": "pricing",
    }
    return {_KEY_MAP.get(k, k): v for k, v in raw.items()}


# ======================================================================
# Django-like AppConfig / ModuleConfig (new contract, Fase 3+)
# ======================================================================


class AppConfig:
    """
    Base class for core app configurations.

    A dev writes ``apps/<name>/app.py`` with a subclass of ``AppConfig``.
    The discovery scanner (hotframe.discovery.scanner) auto-loads it at
    boot time. Core apps are STATIC: they are registered once and do not
    unmount in runtime (that is for ``ModuleConfig`` — see below).

    Subclass attributes:
        name: required, identifier for the app (matches the directory name)
        verbose_name: human-readable label
        mount_prefix: where to mount the app's urlpatterns (default ``/<name>/``)
        version: semver string
        depends: list of other app names this one requires at boot
        permissions: list of (code, label) tuples (optional)
        role_permissions: dict[role_name, list[permission_code | "*"]]
        menu: dict with ``label``, ``icon``, ``order`` (optional)
        navigation: list of dicts (optional)
        is_builtin: True if this app ships with the host application and cannot be disabled.
            Built-in apps are part of the application's core surface, not user-installable plugins.

    Subclass methods:
        async def ready(self) -> None:
            Hook called once after all apps are loaded and mounted.
            Typical use: import signals module to register @receiver
            decorators.

    Runtime usage (pseudo):
        cfg = MyAppConfig()
        registry.register(cfg)
        await cfg.ready()

    Usage::

        class MyAppConfig(AppConfig):
            name = "myapp"
            label = "My App"
    """

    name: str = ""
    verbose_name: str = ""
    mount_prefix: str = ""  # if empty, defaults to f"/{name}/"
    media_path: str = ""  # Media subdirectory name. If empty, uses app name.
    version: str = "0.1.0"
    depends: list[str] = []
    permissions: list[tuple[str, str]] = []
    role_permissions: dict[str, list[str]] = {}
    menu: dict | None = None
    navigation: list[dict] = []
    is_builtin: bool = False

    # Abstract/base subclasses (like ModuleConfig) set this to True to
    # opt out of the required-name check. Concrete subclasses inherit
    # the default False and must declare 'name'.
    _abstract: bool = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Reset the abstract flag for every subclass unless explicitly
        # declared in the class body. This prevents the flag from
        # leaking from an abstract base (e.g. ModuleConfig) into concrete
        # subclasses of it.
        if "_abstract" not in cls.__dict__:
            cls._abstract = False
        if cls._abstract:
            return
        if not cls.name:
            raise ValueError(f"{cls.__name__}: AppConfig subclass must define 'name'")

    async def ready(self) -> None:
        """Hook. Default is no-op. Subclasses override to wire signals."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} version={self.version!r}>"


class ModuleConfig(AppConfig):
    """
    Base class for dynamic modules downloaded from S3.

    A dev writes ``modules/<name>/module.py`` with a subclass of
    ``ModuleConfig``. The module runtime (hotframe.engine.module_runtime)
    installs/activates/deactivates/uninstalls these at runtime.

    Additional subclass attributes vs AppConfig:
        requires_restart: if True, changes to this module cannot be
            hot-mounted and trigger a GracefulRestartCoordinator swap.
        is_system: if True, cannot be uninstalled from UI (e.g. assistant).
        s3_key: optional explicit S3 key override; default is computed
            from name+version by S3ModuleSource.
        sha256: optional explicit SHA256 override.

    Additional subclass methods:
        async def install(self, ctx) -> None:
            Called once when the module is first installed on a hub.
            Use for seeding initial data.
        async def uninstall(self, ctx) -> None:
            Called when the module is uninstalled.
            Use for idempotent cleanup.
        async def activate(self, ctx) -> None:
            Called when the module is activated (after install or when
            re-enabled from the disabled state).
        async def deactivate(self, ctx) -> None:
            Called when the module is deactivated (user disables it).

    Usage::

        class MyModuleConfig(ModuleConfig):
            module_id = "mymodule"
            has_views = True
    """

    # ModuleConfig itself is an abstract base — only its subclasses are
    # expected to carry a name.
    _abstract: bool = True

    requires_restart: bool = False
    is_system: bool = False
    has_views: bool = True
    has_api: bool = True
    media_path: str = ""  # Media subdirectory name. If empty, uses module name.
    s3_key: str | None = None
    sha256: str | None = None

    async def install(self, ctx: Any) -> None:
        """Hook. Called once at first install. Default no-op."""
        return None

    async def uninstall(self, ctx: Any) -> None:
        """Hook. Idempotent cleanup. Default no-op."""
        return None

    async def activate(self, ctx: Any) -> None:
        """Hook. Called on activate. Default no-op."""
        return None

    async def deactivate(self, ctx: Any) -> None:
        """Hook. Called on deactivate. Default no-op."""
        return None
