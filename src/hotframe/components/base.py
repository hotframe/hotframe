# SPDX-License-Identifier: Apache-2.0
"""
Component base class — Pydantic-backed prop schema for Python components.

A component with typed props subclasses :class:`Component` in its
``component.py`` file. The class body declares fields like a normal
Pydantic model; the framework uses it to validate the keyword arguments
passed to ``render_component(name, **props)`` and ``{% component 'name'
... %}`` at render time.

Template-only components (a directory containing just ``template.html``)
do NOT declare this class — Jinja2 handles defaults natively with
``{{ var | default('x') }}``.

Example::

    from hotframe.components import Component

    class MediaPicker(Component):
        path: str
        multiple: bool = False
        accept: str = "image/*"

        def context(self) -> dict:
            # Derived values available to the template alongside props.
            return {"accept_list": self.accept.split(",")}
"""

from __future__ import annotations

from pydantic import BaseModel


class Component(BaseModel):
    """
    Base class for Python-declared components.

    Subclass and declare typed props as Pydantic fields. The discovery
    subsystem finds the subclass in a component's ``component.py`` file
    and wires it as the ``props_cls`` on the registered
    :class:`ComponentEntry`.

    Override :meth:`context` to expose additional variables derived from
    props. The returned dict is merged with the validated props before
    the template is rendered, so the template can reference both.
    """

    model_config = {"arbitrary_types_allowed": True}

    def context(self) -> dict:
        """
        Return extra template context derived from validated props.

        Default returns an empty dict, so the template only sees the
        declared prop fields. Override to compute derived values. This
        method is sync-only: hotframe's Jinja2 environment is sync.
        """
        return {}
