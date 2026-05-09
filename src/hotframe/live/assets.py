# SPDX-License-Identifier: Apache-2.0
"""
Asset helpers exposed to templates as Jinja2 globals.

The runtime serves the live client and morphdom from
``/static/hotframe/`` (see :func:`hotframe.bootstrap.create_app`).
Templates do not need to know that path — they call
``{{ live_assets() }}`` once in ``<head>`` and get the right
``<script>`` tags emitted, including the CSP nonce when present.
"""

from __future__ import annotations

from jinja2 import pass_context
from markupsafe import Markup

# Where the runtime mounts the live client. Hotframe bootstrap is the
# only writer of this path; templates are not free to override it.
LIVE_STATIC_BASE = "/static/hotframe"


@pass_context
def live_assets(ctx) -> Markup:
    """Return the ``<script>`` tags needed for live components.

    Emits two scripts:

    - ``morphdom.min.js`` — the DOM diff used to apply patches.
    - ``live.js`` — the WebSocket client, event bridge, and patch loop.

    Both are served from ``/static/hotframe/`` (mounted by the boot
    sequence). The CSP nonce, when present in the template context, is
    propagated so the scripts pass a strict CSP. Caching is on the
    ``CachedStaticFiles`` mount, so the browser revalidates only on
    deploy.
    """
    nonce = ctx.get("csp_nonce") or ""
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    return Markup(
        f'<script{nonce_attr} src="{LIVE_STATIC_BASE}/morphdom.min.js"></script>\n'
        f'<script{nonce_attr} src="{LIVE_STATIC_BASE}/live.js"></script>'
    )
