"""
Runtime ASGI entry point.

Usage: ``uvicorn hotframe.asgi:application``

Reverse-proxy header rewriting is handled by uvicorn's
``--proxy-headers`` flag for the standard X-Forwarded-* case. The
hotframe-specific ECS-slug rewrite (``hotframe.middleware.proxy_fix``)
is opt-in via ``settings.PROXY_FIX_ENABLED`` and added by ``create_app``
when enabled.
"""

from hotframe.bootstrap import create_app

application = create_app()
