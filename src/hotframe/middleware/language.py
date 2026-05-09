"""
Language detection and activation middleware.

Detection order:
1. Session key ``language``
2. Cookie ``_lang``
3. ``Accept-Language`` header (best match)
4. Authenticated user preference (``request.state.user.language``)
5. Settings default ``LANGUAGE``

On each request the detected language is:
- Validated against SUPPORTED_LANGUAGES
- Activated via ``activate()`` (sets contextvar for the request)
- Stored on ``request.state.language``
- Persisted to the ``_lang`` cookie if it changed
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from hotframe.middleware.i18n_support import SUPPORTED_LANGUAGES, activate, deactivate

logger = logging.getLogger(__name__)

COOKIE_NAME = "_lang"
COOKIE_MAX_AGE = 86400 * 365  # 1 year
_SUPPORTED_CODES = frozenset(code for code, _ in SUPPORTED_LANGUAGES)


def _parse_accept_language(header: str) -> str | None:
    """
    Parse the ``Accept-Language`` header and return the best supported match.

    Handles formats like ``en-US,en;q=0.9,es;q=0.8``.
    Returns ``None`` if no supported language is found.
    """
    if not header:
        return None

    candidates: list[tuple[str, float]] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            lang, _, q = part.partition(";q=")
            try:
                quality = float(q.strip())
            except ValueError:
                quality = 0.0
        else:
            lang = part
            quality = 1.0
        candidates.append((lang.strip().lower(), quality))

    candidates.sort(key=lambda x: x[1], reverse=True)

    for lang, _ in candidates:
        # Try exact match first
        if lang in _SUPPORTED_CODES:
            return lang
        # Try base language (e.g., "en-US" -> "en")
        base = lang.split("-")[0]
        if base in _SUPPORTED_CODES:
            return base

    return None


class LanguageMiddleware(BaseHTTPMiddleware):
    """Detect and activate language for each request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from hotframe.config.settings import get_settings

        settings = get_settings()

        # Skip language detection and cookie for static assets — they are
        # user-agnostic and must not carry Set-Cookie (breaks CDN caching).
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        language: str | None = None

        # 1. Session
        session: dict[str, Any] | None = getattr(request.state, "session", None)
        if session:
            lang = session.get("language")
            if lang and lang in _SUPPORTED_CODES:
                language = lang

        # 2. Cookie
        if language is None:
            cookie_lang = request.cookies.get(COOKIE_NAME)
            if cookie_lang and cookie_lang in _SUPPORTED_CODES:
                language = cookie_lang

        # 3. Accept-Language header
        if language is None:
            accept = request.headers.get("Accept-Language", "")
            language = _parse_accept_language(accept)

        # 4. Authenticated user preference
        if language is None:
            user = getattr(request.state, "user", None)
            if user is not None:
                user_lang = getattr(user, "language", None)
                if user_lang and user_lang in _SUPPORTED_CODES:
                    language = user_lang

        # 5. Settings default
        if language is None:
            language = settings.LANGUAGE

        # Validate and activate
        if language not in _SUPPORTED_CODES:
            logger.warning("Invalid language %r, falling back to default", language)
            language = settings.LANGUAGE
            if language not in _SUPPORTED_CODES:
                language = "en"

        try:
            activate(language)
        except (ValueError, KeyError):
            logger.warning("Failed to activate language %r, falling back to 'en'", language)
            language = "en"
            activate(language)

        # Store on request.state for templates and downstream code
        request.state.language = language

        response = await call_next(request)

        # Set cookie if different from what was sent
        cookie_lang = request.cookies.get(COOKIE_NAME)
        if cookie_lang != language:
            response.set_cookie(
                key=COOKIE_NAME,
                value=language,
                max_age=COOKIE_MAX_AGE,
                path="/",
                httponly=False,
                samesite="lax",
            )

        # Clean up context var
        deactivate()

        return response
