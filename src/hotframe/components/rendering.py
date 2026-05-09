# SPDX-License-Identifier: Apache-2.0
"""
Render components from Jinja2 templates.

Provides :func:`render_component` — a Jinja2 global that looks up a
registered :class:`ComponentEntry`, validates incoming props, builds an
**isolated** context (props + framework slice, no parent scope leakage),
and renders the component's template.

Registered as ``render_component`` on the Jinja2 environment by
:func:`register_component_globals`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from jinja2 import pass_context
from markupsafe import Markup
from pydantic import ValidationError

if TYPE_CHECKING:
    from jinja2 import Environment
    from jinja2.runtime import Context

    from hotframe.components.entry import ComponentEntry
    from hotframe.components.registry import ComponentRegistry

logger = logging.getLogger(__name__)


# Keys copied from the parent Jinja2 context into the component context.
# Everything else is deliberately left behind — component rendering is
# isolated by default so parent-scope variables do not silently bleed
# into sub-templates.
_FRAMEWORK_CONTEXT_KEYS = (
    "request",
    "csrf_token",
    "csp_nonce",
    "user",
    "current_path",
)


def _registry_from_context(ctx: Context) -> ComponentRegistry | None:
    """Look up the component registry injected into ``env.globals``."""
    registry = ctx.environment.globals.get("_hotframe_components")
    if registry is None:
        logger.warning(
            "render_component called before ComponentRegistry was bound to "
            "the Jinja2 environment. Bootstrap injects it at startup."
        )
        return None
    # Bootstrap binds the actual ComponentRegistry instance — narrow ``object``
    # for callers without paying the cost of an isinstance check on the hot
    # path. A wrong type here is a bootstrap bug, not a runtime concern.
    return registry  # type: ignore[return-value]


def _framework_slice(ctx: Context) -> dict:
    """Return the isolated framework-scoped context."""
    return {key: ctx.get(key) for key in _FRAMEWORK_CONTEXT_KEYS if key in ctx}


def _render_entry(
    env: Environment,
    ctx: Context,
    entry: ComponentEntry,
    props: dict,
    body: str | None = None,
) -> Markup:
    """
    Render a :class:`ComponentEntry` with an isolated context.

    Validates props against ``entry.props_cls`` when present, merges the
    framework slice, attaches ``body`` (for ``{% component %}`` tag
    callers), and renders the entry's template.
    """
    render_fn = entry.render_fn
    context: dict[str, Any]
    if render_fn is not None:
        try:
            context = render_fn(**props)
        except ValidationError as exc:
            logger.warning("Component %r prop validation failed: %s", entry.name, exc)
            return Markup(
                f"<!-- component {entry.name!r}: invalid props ({exc.error_count()} error(s)) -->"
            )
        except TypeError as exc:
            logger.warning("Component %r received unexpected kwargs: %s", entry.name, exc)
            return Markup(f"<!-- component {entry.name!r}: unexpected kwargs -->")
    else:
        # Template-only component — pass the raw kwargs through.
        context = dict(props)

    # Inject the framework slice (request, csrf_token, csp_nonce, ...).
    context.update(_framework_slice(ctx))

    # The `body` kwarg is reserved for the `{% component %}` tag body.
    if body is not None:
        context["body"] = Markup(body)

    try:
        template = env.get_template(entry.template)
    except Exception:
        logger.exception("Component %r: could not load template %r", entry.name, entry.template)
        return Markup("")

    return Markup(template.render(**context))


@pass_context
def render_component(ctx: Context, __component_name__: str, /, **props) -> Markup:
    """
    Render a registered component by name.

    The component identifier is a positional-only parameter so callers
    are free to pass a ``name=...`` prop without colliding with the
    dispatch signature.

    Context is isolated: the component receives only the validated props
    plus a well-defined framework slice (``request``, ``csrf_token``,
    ``csp_nonce``, ``user``, ``current_path``). Parent template
    variables do not leak in.

    Passing reserved Python words as kwargs (e.g. ``class``) is not
    supported by Jinja2. Use ``attrs={...}`` for arbitrary HTML
    attributes instead.

    Unknown component names log a warning and return an empty
    :class:`Markup` so templates never crash on typos in production.
    """
    registry = _registry_from_context(ctx)
    if registry is None:
        return Markup("")

    entry = registry.get(__component_name__)
    if entry is None:
        logger.warning("Unknown component %r", __component_name__)
        return Markup("")

    return _render_entry(ctx.environment, ctx, entry, props)


def register_component_globals(env: Environment) -> None:
    """
    Install component-related entries on a Jinja2 :class:`Environment`.

    Currently registers:

    - ``render_component`` global — function-form invocation.

    The ``{% component %}`` tag is registered separately via
    :class:`~hotframe.components.jinja_ext.ComponentExtension` at
    environment creation time (extensions cannot be added after the
    fact without rebuilding the lexer cache).
    """
    env.globals["render_component"] = render_component
