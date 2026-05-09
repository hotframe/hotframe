# SPDX-License-Identifier: Apache-2.0
"""
Jinja2 global functions and filters.

Adds the helpers templates rely on at render time: ``static`` and
``url_for`` for asset / route resolution, ``icon`` for Iconify
markup, ``stat_card`` for dashboard tiles, plus a small set of
filters (``currency``, ``dateformat``, ``timesince``, ``slugify`` …).
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from markupsafe import Markup

if TYPE_CHECKING:
    from jinja2 import Environment


def register_extensions(env: Environment) -> None:
    """Register global functions and filters on the Jinja2 environment."""
    from hotframe.middleware.i18n_support import get_current_language, ngettext

    env.globals.update(
        {
            "static": static_url,
            "url_for": url_for_helper,
            "icon": render_icon,
            "render_slot": render_slot_helper,
            "currency": currency_filter,
            "ngettext": ngettext,
            "get_current_language": get_current_language,
            "csrf_input": lambda: Markup(""),
            "stat_card": stat_card_helper,
        }
    )

    env.filters.update(
        {
            "currency": currency_filter,
            "dateformat": dateformat_filter,
            "timeformat": timeformat_filter,
            "timesince": timesince_filter,
            "truncatewords": truncatewords_filter,
            "slugify": slugify_filter,
        }
    )


# ---------------------------------------------------------------------------
# Global functions
# ---------------------------------------------------------------------------


def static_url(path: str) -> str:
    return f"/static/{path}"


def url_for_helper(name: str, **kwargs: str) -> str:
    if ":" in name:
        module_id, view_id = name.split(":", 1)
    elif "." in name:
        module_id, view_id = name.split(".", 1)
    else:
        return f"/{name}"

    if view_id:
        base = f"/m/{module_id}/{view_id}/"
    else:
        base = f"/m/{module_id}/"

    if kwargs:
        first_val = next(iter(kwargs.values()))
        if first_val is not None and str(first_val):
            base = f"/m/{module_id}/{view_id}/{first_val}/"

    return base


def render_icon(name: str, size: int | None = None, css_class: str = "", **attrs: str) -> Markup:
    if "class" in attrs:
        extra = attrs.pop("class")
        css_class = f"{css_class} {extra}".strip() if css_class else extra

    prefix = "ion"
    icon_name = name
    if ":" in name:
        ns, icon_name = name.split(":", 1)
        prefix = _NAMESPACE_MAP.get(ns, ns)

    classes = "iconify"
    if css_class:
        classes = f"iconify {css_class}"

    parts = [f'<span class="{classes}" data-icon="{prefix}:{icon_name}"']
    if size is not None:
        parts.append(f' data-width="{size}" data-height="{size}"')

    for key, value in attrs.items():
        if value is not None:
            parts.append(f' {key.replace("_", "-")}="{value}"')

    parts.append(' aria-hidden="true"></span>')
    return Markup("".join(parts))


_NAMESPACE_MAP = {
    "ion": "ion",
    "material": "mdi",
    "hero": "heroicons",
    "tabler": "tabler",
    "lucide": "lucide",
    "fa": "fa-solid",
}


def stat_card_helper(
    value: object = "",
    label: str = "",
    icon: str = "",
    color: str = "primary",
    **kwargs: object,
) -> Markup:
    icon_html = render_icon(icon, size=24) if icon else ""
    return Markup(
        f'<div class="stat-card stat-card--{color}">'
        f'  <div class="stat-card__icon">{icon_html}</div>'
        f'  <div class="stat-card__body">'
        f'    <div class="stat-card__value">{value}</div>'
        f'    <div class="stat-card__label">{label}</div>'
        f"  </div>"
        f"</div>"
    )


def render_slot_helper(slot_name: str, **context: object) -> Markup:
    return Markup(f"<!-- slot:{slot_name} -->")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def slugify_filter(value: object) -> str:
    import re
    import unicodedata

    text = str(value) if value is not None else ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def currency_filter(
    value: object,
    currency_code: str | None = None,
    language: str | None = None,
) -> str:
    if currency_code is None:
        from hotframe.config.settings import get_settings

        currency_code = get_settings().CURRENCY
    if language is None:
        from hotframe.middleware.i18n_support import get_current_language

        language = get_current_language()

    try:
        from babel.numbers import format_currency  # type: ignore[import-not-found]

        return format_currency(float(value), currency_code, locale=language)  # type: ignore[arg-type]
    except (ImportError, Exception):
        try:
            return f"{float(value):.2f} {currency_code}"  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return f"{value} {currency_code}"


def dateformat_filter(value: object, fmt: str = "d/m/Y H:i") -> str:
    if value is None:
        return ""

    _PHP_TO_STRFTIME = {
        "d": "%d",
        "j": "%-d",
        "m": "%m",
        "n": "%-m",
        "Y": "%Y",
        "y": "%y",
        "H": "%H",
        "G": "%-H",
        "i": "%M",
        "s": "%S",
    }

    py_fmt = ""
    for ch in fmt:
        py_fmt += _PHP_TO_STRFTIME.get(ch, ch)

    try:
        return value.strftime(py_fmt)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return str(value)


def timeformat_filter(value: object, fmt: str = "H:i") -> str:
    """Format a datetime/time value to a time-only string using PHP-style tokens.

    Supports the subset of dateformat tokens that make sense for time:
    H (24h hour, zero-padded), G (24h hour, no pad), h (12h hour, zero-padded),
    g (12h hour, no pad), i (minute, zero-padded), s (second, zero-padded),
    a (am/pm lowercase), A (AM/PM uppercase).
    """
    if value is None:
        return ""

    _PHP_TO_STRFTIME = {
        "H": "%H",
        "G": "%-H",
        "h": "%I",
        "g": "%-I",
        "i": "%M",
        "s": "%S",
        "a": "%p",
        "A": "%p",
    }

    py_fmt = ""
    for ch in fmt:
        py_fmt += _PHP_TO_STRFTIME.get(ch, ch)

    try:
        result = value.strftime(py_fmt)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return str(value)

    if "a" in fmt:
        result = result.lower()
    return result


def timesince_filter(value: object) -> str:
    if value is None:
        return ""

    from datetime import datetime

    try:
        now = datetime.now(UTC) if getattr(value, "tzinfo", None) is not None else datetime.now()
        delta = now - value  # type: ignore[operator]
    except (TypeError, AttributeError):
        return str(value)

    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0

    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''}"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''}"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''}"


def truncatewords_filter(value: object, count: int = 10, suffix: str = "\u2026") -> str:
    if value is None:
        return ""
    text = str(value)
    words = text.split()
    if len(words) <= count:
        return text
    return " ".join(words[:count]) + suffix
