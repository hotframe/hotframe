# SPDX-License-Identifier: Apache-2.0
"""
Framework event catalog — Pydantic-typed events for framework signals.

Application-specific events should be defined in the application code
by subclassing ``BaseEvent`` and decorating with ``@register_event``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from hotframe.signals.types import BaseEvent, register_event

# ---------------------------------------------------------------------------
# Model lifecycle (emitted by ORM event listeners)
# ---------------------------------------------------------------------------


@register_event
class ModelPreSaveEvent(BaseEvent):
    """Emitted before a model instance is saved (insert or update)."""

    event_name = "model.pre_save"

    model_name: str
    instance_id: UUID | str | int | None = None
    created: bool = False
    changes: dict[str, Any] = {}


@register_event
class ModelPostSaveEvent(BaseEvent):
    """Emitted after a model instance is saved (insert or update)."""

    event_name = "model.post_save"

    model_name: str
    instance_id: UUID | str | int | None = None
    created: bool = False
    changes: dict[str, Any] = {}


@register_event
class ModelPreDeleteEvent(BaseEvent):
    """Emitted before a model instance is deleted."""

    event_name = "model.pre_delete"

    model_name: str
    instance_id: UUID | str | int | None = None


@register_event
class ModelPostDeleteEvent(BaseEvent):
    """Emitted after a model instance is deleted."""

    event_name = "model.post_delete"

    model_name: str
    instance_id: UUID | str | int | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@register_event
class AuthLoginEvent(BaseEvent):
    """Emitted when a user logs in."""

    event_name = "auth.login"

    user_id_auth: UUID
    method: str = "password"


@register_event
class AuthLogoutEvent(BaseEvent):
    """Emitted when a user logs out."""

    event_name = "auth.logout"

    user_id_auth: UUID


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@register_event
class ModuleInstalledEvent(BaseEvent):
    """Emitted when a module is installed."""

    event_name = "modules.installed"

    module_id: str
    version: str = ""


@register_event
class ModuleActivatedEvent(BaseEvent):
    """Emitted when an installed module is activated."""

    event_name = "modules.activated"

    module_id: str
    version: str = ""


@register_event
class ModuleDeactivatedEvent(BaseEvent):
    """Emitted when a module is deactivated."""

    event_name = "modules.deactivated"

    module_id: str
    version: str = ""


@register_event
class ModuleUpdatedEvent(BaseEvent):
    """Emitted when a module is updated to a new version."""

    event_name = "modules.updated"

    module_id: str
    previous_version: str = ""
    new_version: str = ""


@register_event
class ModuleUninstalledEvent(BaseEvent):
    """Emitted when a module is completely removed."""

    event_name = "modules.uninstalled"

    module_id: str
    version: str = ""


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@register_event
class SyncStartedEvent(BaseEvent):
    """Emitted when a sync operation begins."""

    event_name = "sync.started"

    sync_type: str = ""
    target: str = ""


@register_event
class SyncCompletedEvent(BaseEvent):
    """Emitted when a sync operation completes successfully."""

    event_name = "sync.completed"

    sync_type: str = ""
    target: str = ""
    records_synced: int = 0


@register_event
class SyncFailedEvent(BaseEvent):
    """Emitted when a sync operation fails."""

    event_name = "sync.failed"

    sync_type: str = ""
    target: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------


@register_event
class PrintRequestedEvent(BaseEvent):
    """Emitted when a print job is requested."""

    event_name = "print.requested"

    job_id: UUID | None = None
    document_type: str = ""
    printer_id: str | None = None


@register_event
class PrintCompletedEvent(BaseEvent):
    """Emitted when a print job completes successfully."""

    event_name = "print.completed"

    job_id: UUID | None = None
    document_type: str = ""


@register_event
class PrintFailedEvent(BaseEvent):
    """Emitted when a print job fails."""

    event_name = "print.failed"

    job_id: UUID | None = None
    document_type: str = ""
    error: str = ""
