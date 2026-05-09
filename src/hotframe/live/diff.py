# SPDX-License-Identifier: Apache-2.0
"""
Render a :class:`LiveComponent` instance to HTML.

The MVP strategy is "no diff" — we always emit the full component HTML
and let morphdom on the client patch only what changed. This is
simpler and far more debuggable than an AST tag-and-track scheme; the
extra bandwidth is the trade-off, and morphdom's DOM-level diff is
fast enough that it's never the bottleneck.

The wrapper element ``<div data-hf-cid=... data-hf-component=...>`` is
emitted here too. It is the morphdom target on the client and the
``[data-hf-cid]`` selector the discovery code on the client uses to
find live components in the DOM at attach time.
"""

from __future__ import annotations

import html
import json
import logging
from typing import TYPE_CHECKING

from markupsafe import Markup

if TYPE_CHECKING:
    from jinja2 import Environment

    from hotframe.components.entry import ComponentEntry
    from hotframe.live.base import LiveComponent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inner template render
# ---------------------------------------------------------------------------


def render_component_inner(
    env: Environment,
    entry: ComponentEntry,
    instance: LiveComponent,
) -> str:
    """Render the component's template against its render context.

    This is the inner HTML — without the ``data-hf-cid`` wrapper. The
    wrapper is added by :func:`wrap_with_envelope` once at cold-load
    time. On subsequent re-renders the runtime sends only the inner
    HTML, and morphdom replaces the wrapper's children.
    """
    try:
        template = env.get_template(entry.template)
    except Exception:
        logger.exception(
            "LiveComponent %r: failed to load template %r",
            entry.name,
            entry.template,
        )
        return ""

    context = instance.render_context()

    try:
        return template.render(**context)
    except Exception:
        logger.exception(
            "LiveComponent %r: render raised — returning empty patch",
            entry.name,
        )
        return ""


# ---------------------------------------------------------------------------
# Cold-load wrapper
# ---------------------------------------------------------------------------


def _serialise_props(instance: LiveComponent, prop_names: list[str]) -> str:
    """JSON-serialise the props sub-set of the model.

    Only the declared props (not state) are serialised. The client
    sends the same dict back in the ``attach`` envelope so the server
    can rebuild the exact instance after a reconnect.
    """
    raw = instance.model_dump(include=set(prop_names)) if prop_names else {}
    return json.dumps(raw, default=str, separators=(",", ":"))


def wrap_with_envelope(
    inner_html: str,
    *,
    cid: str,
    component_name: str,
    instance: LiveComponent,
    prop_names: list[str],
) -> str:
    """Wrap the inner HTML with the ``data-hf-cid`` envelope.

    The envelope is what the client looks for to know which DOM
    fragments are live components. Attributes:

    - ``data-hf-cid``: instance id, must match the WS protocol's ``cid``.
    - ``data-hf-component``: wire name; tells the server which class to
      reinstantiate on reconnect.
    - ``data-hf-props``: JSON of the original props, used on reconnect.

    The element is a plain ``<div>``. Components that need a different
    root (a ``<tr>``, a ``<li>``) should render the inner template into
    that element themselves and document that the runtime adds a
    wrapping ``<div>`` around it. The trade-off keeps the runtime
    schema-agnostic.
    """
    props_json = _serialise_props(instance, prop_names)
    # html.escape covers the props_json string for double-quote-attr safety.
    props_attr = html.escape(props_json, quote=True)
    return (
        f'<div data-hf-cid="{html.escape(cid, quote=True)}"'
        f' data-hf-component="{html.escape(component_name, quote=True)}"'
        f' data-hf-props="{props_attr}">'
        f"{inner_html}"
        f"</div>"
    )


def render_initial_html(
    env: Environment,
    entry: ComponentEntry,
    instance: LiveComponent,
    *,
    prop_names: list[str],
) -> Markup:
    """Render the cold-load HTML including the envelope.

    Used by the JinjaX extension to expand ``<TodoList ... />`` at
    cold-load time. Returns Markup so Jinja2 does not double-escape.
    """
    inner = render_component_inner(env, entry, instance)
    wrapped = wrap_with_envelope(
        inner,
        cid=instance.cid,
        component_name=instance.component_name or entry.name,
        instance=instance,
        prop_names=prop_names,
    )
    return Markup(wrapped)
