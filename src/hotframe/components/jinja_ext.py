# SPDX-License-Identifier: Apache-2.0
"""
Jinja2 extension — the ``{% component %}`` tag.

Companion to the ``render_component(name, **props)`` global. The tag
variant supports a body block that becomes the ``body`` variable inside
the component template::

    {% component 'modal' title='Confirm' size='md' %}
        <p>Are you sure?</p>
    {% endcomponent %}

Inside ``modal/template.html``::

    <div class="modal modal-{{ size | default('md') }}">
        <h2>{{ title }}</h2>
        <div class="modal-body">{{ body }}</div>
    </div>

Jinja2 does not accept Python reserved words as call kwargs, so HTML
attributes like ``class`` must be passed via a dict::

    {% component 'button' attrs={'class': 'btn-primary', 'id': 'submit'} %}
        Save
    {% endcomponent %}
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING

from jinja2 import nodes
from jinja2.ext import Extension
from markupsafe import Markup

from hotframe.components.rendering import _render_entry

if TYPE_CHECKING:
    from jinja2 import Environment
    from jinja2.parser import Parser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Current-render-context shim
# ---------------------------------------------------------------------------
#
# Jinja2's ``CallBlock`` helpers do not receive the calling ``Context``.
# To give the ``{% component %}`` tag access to the framework slice
# (``request``, ``csrf_token``, ``csp_nonce``, ...) we publish the live
# Context on a ContextVar the moment it is constructed. The tracking
# context class is installed by :func:`install_component_context_tracker`
# at environment setup time.
# ---------------------------------------------------------------------------


_current_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "hotframe_component_render_ctx", default=None
)


class _EmptyCtx:
    """Dict-like stand-in used when no real Jinja2 Context is available."""

    environment = None

    def get(self, _key, default=None):
        return default

    def __contains__(self, _key) -> bool:
        return False


def _current_render_context():
    """Return the currently rendering Jinja2 Context or an empty stand-in."""
    ctx = _current_ctx.get()
    if ctx is None:
        return _EmptyCtx()
    return ctx


def install_component_context_tracker(env: Environment) -> None:
    """
    Patch ``env.context_class`` so the live :class:`Context` is published
    on a :class:`contextvars.ContextVar` during rendering.

    Idempotent — safe to call multiple times on the same environment.
    """
    original_context_class = env.context_class
    if getattr(original_context_class, "_hotframe_patched", False):
        return

    class _TrackingContext(original_context_class):  # type: ignore[valid-type, misc]
        _hotframe_patched = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _current_ctx.set(self)

    env.context_class = _TrackingContext


# ---------------------------------------------------------------------------
# Tag extension
# ---------------------------------------------------------------------------


class ComponentExtension(Extension):
    """Jinja2 tag ``{% component 'name' key=val ... %}...{% endcomponent %}``."""

    tags = {"component"}

    def parse(self, parser: Parser):
        lineno = next(parser.stream).lineno

        # First argument: the component name (any expression — typically a string literal).
        name_expr = parser.parse_expression()

        # Collect keyword arguments until the block end.
        kwargs: list[nodes.Keyword] = []
        while parser.stream.current.type != "block_end":
            if parser.stream.skip_if("comma"):
                continue
            if parser.stream.current.test("name"):
                key = parser.stream.expect("name").value
                parser.stream.expect("assign")
                value = parser.parse_expression()
                kwargs.append(nodes.Keyword(key, value, lineno=value.lineno))
            else:
                # Unknown token — break so Jinja raises a descriptive error.
                break

        # Body between {% component %} and {% endcomponent %}.
        body = parser.parse_statements(("name:endcomponent",), drop_needle=True)

        call = self.call_method("_render_component", [name_expr], kwargs)
        return nodes.CallBlock(call, [], [], body).set_lineno(lineno)

    def _render_component(
        self,
        __component_name__: str,
        /,
        *,
        caller=None,
        **props,
    ) -> Markup:
        """
        Render a component by name with the given props and the body
        produced by ``caller()``.

        The component identifier is declared as a positional-only
        parameter (``__component_name__``) so templates are free to
        pass a ``name=...`` prop without colliding with the dispatch
        signature.

        Uses the component registry injected into the Jinja2 environment
        globals as ``_hotframe_components``. Unknown names log a warning
        and return an empty :class:`Markup` so templates stay resilient.
        """
        env = self.environment
        registry = env.globals.get("_hotframe_components")
        if registry is None:
            logger.warning("{%% component %%} used before ComponentRegistry was bound to the env")
            return Markup("")

        entry = registry.get(__component_name__)  # type: ignore[attr-defined]
        if entry is None:
            logger.warning(
                "Unknown component %r (via {%% component %%} tag)",
                __component_name__,
            )
            return Markup("")

        body = caller() if caller is not None else ""
        ctx = _current_render_context()
        return _render_entry(env, ctx, entry, props, body=str(body))
