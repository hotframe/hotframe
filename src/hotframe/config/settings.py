# SPDX-License-Identifier: Apache-2.0
"""
Hotframe configuration via Pydantic BaseSettings.

Users subclass ``HotframeSettings`` in their project's ``settings.py``
to add application-specific fields. Hotframe reads framework-level
settings from this base class.

Example project settings::

    from hotframe.config.settings import HotframeSettings

    class Settings(HotframeSettings):
        model_config = SettingsConfigDict(env_prefix="MY_APP_")
        MY_CUSTOM_FIELD: str = "value"

    settings = Settings()
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HotframeSettings(BaseSettings):
    """Base settings for hotframe applications.

    Subclass this in your project's ``settings.py`` and override
    ``model_config`` to set your own ``env_prefix``.

    Framework-level fields (DATABASE_URL, SECRET_KEY, DEBUG, etc.) are
    always available.  Application-specific fields (user model, tenant
    identity, billing tokens, etc.) are added by the subclass.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Apps ---
    # Extra routers to mount (dotted paths to Router instances).
    # App routers are auto-discovered from apps/; use this for standalone routers.
    EXTRA_ROUTERS: list[str] = []

    # --- Database ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./app.db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_TIMEOUT: int = 30
    DB_ECHO: bool = False
    # When True, disable asyncpg's auto-prepared-statement cache.
    # Required when DATABASE_URL points at a transaction-mode pooler
    # (AWS RDS Proxy, PgBouncer, Supavisor, GCP Cloud SQL Auth Proxy
    # with pool, etc.) — those rotate the backend connection between
    # transactions, invalidating any prepared statement cached on the
    # client side. Has no effect on non-asyncpg drivers.
    DB_DISABLE_PREPARED_STATEMENTS: bool = False
    MAX_REQUEST_BODY: int = 10 * 1024 * 1024  # 10 MB

    # --- Security ---
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(64))
    SECRETS_KEY: str | None = Field(
        default=None,
        description=(
            "Fernet key for encrypting secrets at rest. Required in production deployments."
        ),
    )
    DEBUG: bool = True

    # --- Modules ---
    MODULES_DIR: Path = Path("./modules")
    MODULES_CACHE_DIR: Path = Path("/tmp/hotframe-modules")

    # --- Module storage ---
    MODULE_SOURCE: str = "filesystem"  # "filesystem", "s3", "http"
    MODULE_MARKETPLACE_URL: str = ""  # e.g. "https://marketplace.example.com/modules"
    S3_MODULES_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # --- Static files ---
    STATIC_ROOT: Path = Path("./static")
    STATIC_URL: str = "/static/"

    # --- Media storage ---
    MEDIA_ROOT: Path = Path("./media")
    MEDIA_STORAGE: str = "local"  # "local" or "s3"
    MEDIA_S3_BUCKET: str = ""
    MEDIA_URL: str = "/media/"  # URL prefix for serving media files

    # --- Deployment ---
    DEPLOYMENT_MODE: Literal["local", "web"] = "local"

    # --- Locale ---
    LANGUAGE: str = "en"
    CURRENCY: str = "USD"

    # --- CORS ---
    # Empty = CORS disabled. Set origins to enable.
    CORS_ORIGINS: list[str] = []  # e.g. ["http://localhost:3000", "https://myapp.com"]
    CORS_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    CORS_HEADERS: list[str] = ["*"]
    CORS_CREDENTIALS: bool = True

    # --- Security policies ---
    CSP_ENFORCE: bool = False

    # --- Logging ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["console", "json"] = "console"

    # --- Middleware ---
    # List of middleware classes in execution order (outermost first).
    # Override in your settings to add/remove middleware.
    MIDDLEWARE: list[str] = [
        "hotframe.middleware.timeout.TimeoutMiddleware",
        "hotframe.middleware.error_pages.ErrorPageMiddleware",
        "hotframe.middleware.body_limit.BodyLimitMiddleware",
        "asgi_correlation_id.CorrelationIdMiddleware",
        "hotframe.middleware.observability.RequestObservabilityMiddleware",
        "hotframe.middleware.rate_limit.APIRateLimitMiddleware",
        # Boundary sits OUTSIDE ``ModuleMiddlewareManager`` (and therefore
        # also outside the module's own router) so it can catch exceptions
        # raised by either layer. It is placed before module-scoped
        # middleware so a buggy module-contributed middleware is also
        # caught, but inside the global request-id/observability layer so
        # captured errors still carry their correlation IDs.
        "hotframe.engine.boundary.ModuleBoundaryMiddleware",
        "hotframe.middleware.module_middleware.ModuleMiddlewareManager",
        "hotframe.auth.csrf.CSRFMiddleware",
        "hotframe.middleware.language.LanguageMiddleware",
        "hotframe.middleware.csp.CSPMiddleware",
        "hotframe.middleware.session_safe.RobustSessionMiddleware",
    ]

    # --- CSRF ---
    CSRF_EXEMPT_PREFIXES: list[str] = [
        "/api/",
        "/health",
        "/static/",
    ]

    # --- Rate limiting ---
    RATE_LIMIT_API: int = 120  # requests per minute
    RATE_LIMIT_AUTH: int = 60
    RATE_LIMIT_AUTH_PREFIXES: list[str] = []

    # --- Session ---
    SESSION_COOKIE_NAME: str = "session"
    SESSION_MAX_AGE: int = 86400 * 30  # 30 days

    # --- CSP ---
    # Trusted Types stays OFF by default. The runtime client (``live.js`` +
    # ``morphdom``) uses ``insertAdjacentHTML``-style patterns that the
    # strict ``require-trusted-types-for 'script'`` directive blocks. Turn
    # it on only when the project ships a Trusted Types policy compatible
    # with that pattern. Defense-in-depth otherwise rests on nonces, CSRF,
    # Jinja autoescape, SameSite cookies, and signed sessions.
    CSP_TRUSTED_TYPES: bool = False
    CSP_ALLOWED_SOURCES: dict[str, list[str]] = {
        "script": [],
        "style": [],
        "connect": [],
        "img": [],
        "font": [],
    }

    # --- Auth ---
    AUTH_USER_MODEL: str = ""  # e.g. "apps.accounts.models.User"
    AUTH_LOGIN_URL: str = "/login"
    AUTH_UNAUTHORIZED_URL: str = "/unauthorized"

    # --- App title ---
    APP_TITLE: str = "Hotframe App"

    # --- Proxy ---
    PROXY_FIX_ENABLED: bool = False
    PROXY_SLUG: str = ""
    PROXY_DOMAIN_BASE: str = ""
    PROXY_AWS_REGION: str = ""

    # --- Observability ---
    OTEL_SERVICE_NAME: str = "hotframe"

    # --- HTTP clients ---
    # When True, AuthenticatedClient instances emit lifecycle events
    # (http.request.started/completed/failed) through the app's EventBus.
    # Off by default: zero cost when not explicitly enabled.
    HTTP_CLIENT_EVENTS: bool = False

    # Filesystem paths scanned on startup for ambient HTTP interceptors.
    # Each ``.py`` file is imported and module-level attributes that
    # satisfy the ``hotframe.http.Interceptor`` protocol are collected
    # into ``app.state.http_interceptors`` and auto-applied to every
    # client registered without an explicit ``interceptors=`` list.
    HTTP_INTERCEPTOR_PATHS: list[str] = []

    # --- Module state model ---
    # Dotted path to the SQLAlchemy model used for module state tracking.
    # Must have: module_id, status, version, manifest, config, error_message,
    # installed_at, activated_at, disabled_at columns.
    # If empty, hotframe uses its built-in Module model.
    MODULE_STATE_MODEL: str = ""

    # --- Global template context ---
    # Dotted path to an async callable ``(request) -> dict`` that returns
    # extra template context merged into every render. Like Django's
    # TEMPLATES[].OPTIONS.context_processors but as a single hook.
    GLOBAL_CONTEXT_HOOK: str = ""

    # --- Permission resolver ---
    # Dotted path to an async callable ``(request, user_id) -> list[str]``
    # that returns permission strings for the user. Used by the ``@view``
    # decorator when a route declares required permissions.
    PERMISSION_RESOLVER: str = ""

    @field_validator("LOG_LEVEL")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        v = v.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}, got {v!r}")
        return v

    @field_validator("MODULES_DIR", "MODULES_CACHE_DIR", mode="before")
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        return Path(v).resolve()

    @model_validator(mode="after")
    def _validate_secrets_key(self) -> HotframeSettings:
        if self.DEPLOYMENT_MODE != "local":
            if not self.SECRETS_KEY:
                raise ValueError(
                    "SECRETS_KEY is required in non-local deployments. "
                    "Generate one with: python -c "
                    "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
                )
        if self.SECRETS_KEY:
            try:
                decoded = base64.urlsafe_b64decode(self.SECRETS_KEY)
            except Exception as exc:
                raise ValueError(f"SECRETS_KEY is not valid url-safe base64: {exc}") from exc
            if len(decoded) != 32:
                raise ValueError(
                    f"SECRETS_KEY must decode to exactly 32 bytes "
                    f"(a valid Fernet key), got {len(decoded)} bytes"
                )
        return self

    @property
    def is_sqlite(self) -> bool:
        """Return True if the database backend is SQLite."""
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        """Return True when running in web mode with DEBUG disabled."""
        return self.DEPLOYMENT_MODE == "web" and not self.DEBUG


_settings: HotframeSettings | None = None


def get_settings() -> HotframeSettings:
    """Return cached singleton settings instance."""
    global _settings
    if _settings is None:
        _settings = HotframeSettings()
    return _settings


def set_settings(settings: HotframeSettings) -> None:
    """Set the settings instance (called by create_app)."""
    global _settings
    _settings = settings


def reset_settings() -> None:
    """Reset cached settings (for testing)."""
    global _settings
    _settings = None
