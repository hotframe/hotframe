# SPDX-License-Identifier: Apache-2.0
"""
CSP utility functions.

Builds Content-Security-Policy header values with per-request nonces.
Allowed sources can be added via ``settings.CSP_ALLOWED_SOURCES``.
"""

from __future__ import annotations


def build_csp_header(nonce: str, enforce: bool) -> tuple[str, str]:
    """
    Build the CSP header name and value.

    Allowed sources from ``settings.CSP_ALLOWED_SOURCES`` are appended
    to the base directives for each resource type.

    Args:
        nonce: Per-request nonce token.
        enforce: If True, use ``Content-Security-Policy`` (blocking).
                 If False, use ``Content-Security-Policy-Report-Only``.

    Returns:
        Tuple of (header_name, header_value).
    """
    from hotframe.config.settings import get_settings

    settings = get_settings()
    sources = settings.CSP_ALLOWED_SOURCES

    extra_script = " ".join(sources.get("script", []))
    extra_style = " ".join(sources.get("style", []))
    extra_connect = " ".join(sources.get("connect", []))
    extra_img = " ".join(sources.get("img", []))
    extra_font = " ".join(sources.get("font", []))

    if enforce:
        connect_src = f"connect-src 'self' wss://* {extra_connect}".strip()
    else:
        connect_src = f"connect-src 'self' ws://localhost:* wss://* {extra_connect}".strip()

    directives = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval' {extra_script}".strip(),
        f"style-src 'self' 'unsafe-inline' {extra_style}".strip(),
        f"img-src 'self' data: blob: {extra_img}".strip(),
        connect_src,
        f"font-src 'self' {extra_font}".strip(),
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]

    if settings.CSP_TRUSTED_TYPES:
        directives.append("require-trusted-types-for 'script'")
        # Allow the named Trusted Types policies hotframe relies on:
        # ``default`` covers the permissive base policy installed by the
        # project base template; ``iconify`` is the policy the Iconify
        # CDN script registers. Add more names via a settings hook if a
        # downstream library needs them.
        trusted_policies = "default iconify"
        directives.append(f"trusted-types {trusted_policies} 'allow-duplicates'")

    header_name = "Content-Security-Policy" if enforce else "Content-Security-Policy-Report-Only"
    header_value = "; ".join(directives)

    return header_name, header_value
