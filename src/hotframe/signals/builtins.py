# SPDX-License-Identifier: Apache-2.0
"""
Framework signal constants.

Only includes model lifecycle and framework-level signals.
Application-specific signals should be defined in the application code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hotframe.signals.types import BaseEvent

# ---------------------------------------------------------------------------
# Model lifecycle (emitted by ORM event listeners)
# ---------------------------------------------------------------------------
MODEL_PRE_SAVE = "model.pre_save"
MODEL_POST_SAVE = "model.post_save"
MODEL_PRE_DELETE = "model.pre_delete"
MODEL_POST_DELETE = "model.post_delete"

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_LOGIN = "auth.login"
AUTH_LOGOUT = "auth.logout"

# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------
MODULES_INSTALLED = "modules.installed"
MODULES_ACTIVATED = "modules.activated"
MODULES_DEACTIVATED = "modules.deactivated"
MODULES_UPDATED = "modules.updated"
MODULES_UNINSTALLED = "modules.uninstalled"

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
SYNC_STARTED = "sync.started"
SYNC_COMPLETED = "sync.completed"
SYNC_FAILED = "sync.failed"

# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------
PRINT_REQUESTED = "print.requested"
PRINT_COMPLETED = "print.completed"
PRINT_FAILED = "print.failed"


# ---------------------------------------------------------------------------
# Aggregated collection
# ---------------------------------------------------------------------------
SYSTEM_SIGNALS: dict[str, str] = {
    name: value
    for name, value in globals().items()
    if isinstance(value, str) and not name.startswith("_") and "." in value
}


def _build_signal_map() -> dict[str, type[BaseEvent]]:
    from hotframe.signals.catalog import (
        AuthLoginEvent,
        AuthLogoutEvent,
        ModelPostDeleteEvent,
        ModelPostSaveEvent,
        ModelPreDeleteEvent,
        ModelPreSaveEvent,
        ModuleActivatedEvent,
        ModuleDeactivatedEvent,
        ModuleInstalledEvent,
        ModuleUninstalledEvent,
        ModuleUpdatedEvent,
        PrintCompletedEvent,
        PrintFailedEvent,
        PrintRequestedEvent,
        SyncCompletedEvent,
        SyncFailedEvent,
        SyncStartedEvent,
    )

    return {
        MODEL_PRE_SAVE: ModelPreSaveEvent,
        MODEL_POST_SAVE: ModelPostSaveEvent,
        MODEL_PRE_DELETE: ModelPreDeleteEvent,
        MODEL_POST_DELETE: ModelPostDeleteEvent,
        AUTH_LOGIN: AuthLoginEvent,
        AUTH_LOGOUT: AuthLogoutEvent,
        MODULES_INSTALLED: ModuleInstalledEvent,
        MODULES_ACTIVATED: ModuleActivatedEvent,
        MODULES_DEACTIVATED: ModuleDeactivatedEvent,
        MODULES_UPDATED: ModuleUpdatedEvent,
        MODULES_UNINSTALLED: ModuleUninstalledEvent,
        SYNC_STARTED: SyncStartedEvent,
        SYNC_COMPLETED: SyncCompletedEvent,
        SYNC_FAILED: SyncFailedEvent,
        PRINT_REQUESTED: PrintRequestedEvent,
        PRINT_COMPLETED: PrintCompletedEvent,
        PRINT_FAILED: PrintFailedEvent,
    }


_signal_to_event: dict[str, type[BaseEvent]] | None = None


def get_signal_event_map() -> dict[str, type[BaseEvent]]:
    global _signal_to_event
    if _signal_to_event is None:
        _signal_to_event = _build_signal_map()
    return _signal_to_event


def get_event_class(signal: str) -> type[BaseEvent] | None:
    return get_signal_event_map().get(signal)
