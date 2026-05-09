# SPDX-License-Identifier: Apache-2.0
"""hotframe — Modular Python web framework with hot-mount dynamic modules."""

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Lazy imports — only loaded when accessed
# ---------------------------------------------------------------------------

_LAZY_IMPORTS: dict[str, str] = {
    # Bootstrap
    "create_app": "hotframe.bootstrap",
    # Settings
    "HotframeSettings": "hotframe.config.settings",
    "get_settings": "hotframe.config.settings",
    # Apps
    "AppConfig": "hotframe.apps.config",
    "ModuleConfig": "hotframe.apps.config",
    # Models
    "Base": "hotframe.models.base",
    "Model": "hotframe.models.base",
    "HubBaseModel": "hotframe.models.base",  # backward compat alias for Model
    "TimeStampedModel": "hotframe.models.base",
    "ActiveModel": "hotframe.models.base",
    "HubMixin": "hotframe.models.mixins",
    "TimestampMixin": "hotframe.models.mixins",
    "AuditMixin": "hotframe.models.mixins",
    "SoftDeleteMixin": "hotframe.models.mixins",
    "HubQuery": "hotframe.models.queryset",
    # Repository
    "BaseRepository": "hotframe.repository.base",
    # DB Protocols
    "ISession": "hotframe.db.protocols",
    "IQueryBuilder": "hotframe.db.protocols",
    "IRepository": "hotframe.db.protocols",
    "IExecuteResult": "hotframe.db.protocols",
    "IScalarResult": "hotframe.db.protocols",
    # Signals
    "AsyncEventBus": "hotframe.signals.dispatcher",
    "HookRegistry": "hotframe.signals.hooks",
    "BaseEvent": "hotframe.signals.types",
    "register_event": "hotframe.signals.types",
    # ORM
    "setup_orm_events": "hotframe.orm.events",
    # Views — @view decorator + plain HTTP redirect / refresh / message helpers
    "view": "hotframe.views.responses",
    "is_reactive_request": "hotframe.views.responses",
    "reactive_redirect": "hotframe.views.responses",
    "reactive_refresh": "hotframe.views.responses",
    "reactive_trigger": "hotframe.views.responses",
    "reactive_message": "hotframe.views.responses",
    "htmx_view": "hotframe.views.responses",
    "is_htmx_request": "hotframe.views.responses",
    "htmx_redirect": "hotframe.views.responses",
    "htmx_refresh": "hotframe.views.responses",
    "htmx_trigger": "hotframe.views.responses",
    "add_message": "hotframe.views.responses",
    "sse_stream": "hotframe.views.responses",
    "BroadcastHub": "hotframe.views.broadcast",
    # Templating
    "SlotRegistry": "hotframe.templating.slots",
    # Components
    "Component": "hotframe.components.base",
    "ComponentRegistry": "hotframe.components.registry",
    "ComponentEntry": "hotframe.components.entry",
    # Auth
    "get_session_user_id": "hotframe.auth.auth",
    "hash_password": "hotframe.auth.auth",
    "verify_password": "hotframe.auth.auth",
    "has_permission": "hotframe.auth.permissions",
    "require_permission": "hotframe.auth.permissions",
    # Dependencies
    "DbSession": "hotframe.auth.current_user",
    "CurrentUser": "hotframe.auth.current_user",
    "OptionalUser": "hotframe.auth.current_user",
    "EventBus": "hotframe.auth.current_user",
    "Hooks": "hotframe.auth.current_user",
    "Slots": "hotframe.auth.current_user",
    "get_db": "hotframe.auth.current_user",
    "get_current_user": "hotframe.auth.current_user",
    # Services
    "ModuleService": "hotframe.apps.service_facade",
    "action": "hotframe.apps.service_facade",
    # Engine
    "ModuleStateDB": "hotframe.engine.state",
    "HotMountPipeline": "hotframe.engine.pipeline",
    "ImportManager": "hotframe.engine.import_manager",
    "MarketplaceClient": "hotframe.engine.marketplace_client",
    # Config
    "get_engine": "hotframe.config.database",
    "get_session_factory": "hotframe.config.database",
    # HTTP clients
    "AuthenticatedClient": "hotframe.http",
    "HttpClientRegistry": "hotframe.http",
    "Auth": "hotframe.http",
    "BearerAuth": "hotframe.http",
    "ApiKeyAuth": "hotframe.http",
    "QueryApiKeyAuth": "hotframe.http",
    "BasicAuth": "hotframe.http",
    "HmacAuth": "hotframe.http",
    "CustomAuth": "hotframe.http",
    "NoAuth": "hotframe.http",
    # HTTP interceptors
    "Interceptor": "hotframe.http",
    "InterceptorBase": "hotframe.http",
    "CallNext": "hotframe.http",
    "RetryInterceptor": "hotframe.http",
    "CircuitBreakerInterceptor": "hotframe.http",
    "RefreshInterceptor": "hotframe.http",
    "exponential_backoff": "hotframe.http",
    "discover_interceptors": "hotframe.http",
    # Live runtime — stateful, server-rendered, WebSocket-driven components.
    "LiveComponent": "hotframe.live",
    "LiveSession": "hotframe.live",
    "LiveRuntime": "hotframe.live",
    "event": "hotframe.live",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib

        module = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module 'hotframe' has no attribute {name!r}")


__all__ = [*list(_LAZY_IMPORTS.keys()), "__version__"]
