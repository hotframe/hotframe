# SPDX-License-Identifier: Apache-2.0
"""
Jinja2 ``{% live "name" prop=value ... %}`` tag.

Cold-load entry point for live components. The tag instantiates a
:class:`LiveComponent` subclass, runs ``on_mount`` synchronously,
renders the template against the resulting state, and emits the HTML
wrapped with the ``data-hf-cid`` envelope so the client can attach to
it on WS open.

Why a tag and not a global function? ``render_component()`` already
exists for stateless components, but live components need a different
contract:

- A unique ``cid`` per render call (uuid4).
- An async ``on_mount`` that may hit the DB.
- The envelope wrapper.

Bundling all of that into one tag keeps the template author's job to
``{% live "todo_list" user_id=user.id %}`` — same shape as the
stateless ``{% component %}`` tag.

The async ``on_mount`` is run via ``asyncio.run()`` when the surrounding
template render is sync, or scheduled on the running loop when the
render is itself happening from async code (Jinja2 supports both).
The detection is conservative: if a loop is running in the current
thread we use it; otherwise we spin one up.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from jinja2 import nodes
from jinja2.ext import Extension
from markupsafe import Markup

from hotframe.live.base import LiveComponent
from hotframe.live.diff import render_initial_html

if TYPE_CHECKING:
    from jinja2.parser import Parser

    from hotframe.components.registry import ComponentRegistry

logger = logging.getLogger(__name__)


class LiveExtension(Extension):
    """``{% live 'name' k=v ... %}`` — cold-load a stateful component."""

    tags = {"live"}

    def parse(self, parser: Parser):
        lineno = next(parser.stream).lineno

        # First argument: the component name (string literal expected).
        name_expr = parser.parse_expression()

        # Keyword arguments until block_end.
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
                break

        call = self.call_method("_render_live", [name_expr], kwargs)
        # No body — live components do not accept a `caller()` block.
        # Slot semantics for live can be added later if needed.
        return nodes.Output([call]).set_lineno(lineno)

    def _render_live(self, __component_name__: str, /, **props) -> Markup:
        env = self.environment
        registry: ComponentRegistry | None = env.globals.get("_hotframe_components")
        if registry is None:
            logger.warning("{%% live %%} used before ComponentRegistry was bound")
            return Markup("")

        entry = registry.get(__component_name__)
        if entry is None:
            logger.warning("Unknown live component %r", __component_name__)
            return Markup("")
        if not getattr(entry, "is_live", False):
            logger.warning(
                "Component %r is registered but is not a LiveComponent. "
                "Use {%% component %%} for stateless components.",
                __component_name__,
            )
            return Markup("")

        cls = entry.props_cls
        if cls is None or not isinstance(cls, type) or not issubclass(cls, LiveComponent):
            logger.warning(
                "Live component %r has no LiveComponent class attached", __component_name__
            )
            return Markup("")

        try:
            instance = cls(**props)
        except Exception:
            logger.exception("Live component %r: invalid props", __component_name__)
            return Markup(f"<!-- live {__component_name__!r}: invalid props -->")

        # Stamp identity. The cid generated here is what the client
        # will echo back in its ``attach`` envelope.
        instance._cid = f"c-{uuid.uuid4().hex[:12]}"
        instance._component_name = __component_name__

        # Run on_mount synchronously. We are inside a sync Jinja render
        # (TemplateResponse calls ``template.render`` directly), so we
        # need our own loop for the await.
        try:
            _run_async(instance.on_mount())
        except Exception:
            logger.exception(
                "Live component %r: on_mount failed during cold-load",
                __component_name__,
            )
            return Markup(f"<!-- live {__component_name__!r}: mount failed -->")

        # Snapshot prop names from the model fields. Pydantic v2 keeps
        # field metadata on the class.
        prop_names = list(cls.model_fields.keys())

        return render_initial_html(env, entry, instance, prop_names=prop_names)


def _run_async(coro):
    """Run ``coro`` to completion regardless of whether a loop is active.

    During normal HTTP rendering the request handler is async but Jinja
    is sync; we are off the event loop by the time the template
    renders. Spawn a fresh loop for the call. If a loop happens to be
    running (e.g. tests with ``asyncio.run`` already entered), we run
    on a new thread to avoid deadlocking it.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)

    # A loop is already running in this thread — fall back to a
    # scheduled task on the existing loop. Block by polling the future.
    import threading

    result_box: dict[str, object] = {}

    def runner() -> None:
        result_box["v"] = asyncio.run(coro)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    return result_box.get("v")
