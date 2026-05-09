"""
File-name conventions for app/module discovery.

This module defines the single source of truth for how the discovery
scanner interprets the contents of an ``apps/<name>/`` or
``modules/<name>/`` directory. Each conventional filename maps to a
semantic role in the framework.

The discovery step (in ``hotframe.discovery.scanner``) uses this table
to decide what to import, what to mount, and what to register.

Keeping the conventions here (and not hard-coded inside the scanner)
makes them trivially discoverable, documentable, and test-replaceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    """What kind of artifact a conventional file contributes."""

    ENTRY_POINT = "entry_point"  # app.py or module.py — the AppConfig/ModuleConfig subclass
    MODELS = "models"  # SQLAlchemy model classes
    ROUTES = "routes"  # urlpatterns list (or router fallback)
    API = "api"  # APIRouter for REST endpoints
    SCHEMAS = "schemas"  # Pydantic schemas (no side effects, import only)
    SERVICES = "services"  # ModuleService subclasses (@action)
    REPOSITORY = "repository"  # BaseRepository subclasses
    SIGNALS = "signals"  # @receiver decorated functions (side effect on import)
    MIGRATIONS = "migrations"  # Alembic per-app/module directory
    TEMPLATES = "templates"  # Jinja2 templates directory
    STATIC = "static"  # Static assets directory
    LOCALES = "locales"  # i18n directory
    TESTS = "tests"  # pytest tests directory
    MANAGEMENT = "management"  # management/commands/*.py


@dataclass(frozen=True, slots=True)
class Convention:
    """One row in the convention table.

    ``required_exports`` semantics: if non-empty, the imported module must
    export **at least one** of the listed names. This allows a convention
    to accept multiple equivalent shapes (e.g. ``routes.py`` may expose
    either ``urlpatterns`` for the Django-like contract or ``router`` for
    the FastAPI-style contract).
    """

    filename_or_dir: str  # e.g. "models.py" or "templates"
    kind: Kind
    is_directory: bool = False
    optional: bool = True  # if False, its absence is an error
    required_exports: tuple[str, ...] = ()  # at-least-one-of; empty = no requirement


# Single source of truth. Order matters only for cosmetic logging.
APP_CONVENTIONS: tuple[Convention, ...] = (
    # entry point: exactly one of app.py XOR module.py must exist
    Convention("app.py", Kind.ENTRY_POINT, is_directory=False, optional=True),
    Convention("module.py", Kind.ENTRY_POINT, is_directory=False, optional=True),
    # python files
    Convention("models.py", Kind.MODELS, optional=True),
    # routes.py accepts either the Django-like 'urlpatterns' list or
    # a bare FastAPI 'router' (APIRouter) while the framework transitions.
    Convention("routes.py", Kind.ROUTES, optional=True, required_exports=("urlpatterns", "router")),
    # api.py accepts both 'router' (FastAPI APIRouter) and 'api_router' (legacy alias).
    Convention("api.py", Kind.API, optional=True, required_exports=("router", "api_router")),
    Convention("schemas.py", Kind.SCHEMAS, optional=True),
    Convention("services.py", Kind.SERVICES, optional=True),
    Convention("repository.py", Kind.REPOSITORY, optional=True),
    Convention("signals.py", Kind.SIGNALS, optional=True),
    # directories
    Convention("migrations", Kind.MIGRATIONS, is_directory=True, optional=True),
    Convention("templates", Kind.TEMPLATES, is_directory=True, optional=True),
    Convention("static", Kind.STATIC, is_directory=True, optional=True),
    Convention("locales", Kind.LOCALES, is_directory=True, optional=True),
    Convention("tests", Kind.TESTS, is_directory=True, optional=True),
    Convention("management", Kind.MANAGEMENT, is_directory=True, optional=True),
)


def conventions_by_kind() -> dict[Kind, tuple[Convention, ...]]:
    """Helper: group conventions by their ``Kind``."""
    from collections import defaultdict

    grouped: dict[Kind, list[Convention]] = defaultdict(list)
    for conv in APP_CONVENTIONS:
        grouped[conv.kind].append(conv)
    return {k: tuple(v) for k, v in grouped.items()}
