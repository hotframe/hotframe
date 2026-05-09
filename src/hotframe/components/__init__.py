# SPDX-License-Identifier: Apache-2.0
"""Component subsystem — reusable UI units backed by a registry."""

from hotframe.components.base import Component
from hotframe.components.entry import ComponentEntry
from hotframe.components.mounting import (
    mount_component_routers,
    mount_component_routers_for_module,
    mount_component_static,
    mount_component_static_for_module,
    unmount_component_router,
    unmount_component_routers_for_module,
    unmount_component_static,
    unmount_component_static_for_module,
)
from hotframe.components.registry import ComponentRegistry

__all__ = [
    "Component",
    "ComponentEntry",
    "ComponentRegistry",
    "mount_component_routers",
    "mount_component_routers_for_module",
    "mount_component_static",
    "mount_component_static_for_module",
    "unmount_component_router",
    "unmount_component_routers_for_module",
    "unmount_component_static",
    "unmount_component_static_for_module",
]
