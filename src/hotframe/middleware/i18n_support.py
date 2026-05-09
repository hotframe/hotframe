"""
Internationalization support using gettext + Babel.

Provides:
- Per-request language via contextvars
- Per-module translation domains with fallback to core
- LazyString for deferred translation (used in module.py constants)
- Translation cache (LRU) for performance
- Jinja2-compatible translations adapter

Primary language: English. All _() strings in code are in English.
Spanish is the first translation target.

Fallback chain: module domain -> core domain -> original string.

Usage::

    from hotframe.middleware.i18n_support import _, LazyString, activate, get_current_language

    # In module.py (evaluated at import time, translated at render time)
    MODULE_NAME = LazyString("Inventory", module_id="inventory")

    # In request handlers
    activate("es")
    message = _("Product created")  # -> "Producto creado"

    # Reset to default
    deactivate()
"""

from __future__ import annotations

import gettext as gettext_module
import logging
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"),
    ("es", "Español"),
]

DEFAULT_LANGUAGE = "en"

# ---------------------------------------------------------------------------
# Context-local language
# ---------------------------------------------------------------------------
_current_language: ContextVar[str] = ContextVar("current_language", default=DEFAULT_LANGUAGE)

# ---------------------------------------------------------------------------
# Locale directories
# ---------------------------------------------------------------------------

# Core locales directory: hotframe/locales/
CORE_LOCALES_DIR = Path(__file__).parent.parent.parent / "locales"

# Module locales dirs (populated at runtime by ModuleLoader)
_module_locales: dict[str, Path] = {}  # module_id -> locales path


# ---------------------------------------------------------------------------
# Language activation
# ---------------------------------------------------------------------------


def get_current_language() -> str:
    """Get the current language code for this context."""
    return _current_language.get()


def activate(language: str) -> None:
    """
    Set the current language for this context.

    Args:
        language: ISO 639-1 language code (e.g. ``'en'``, ``'es'``).

    Raises:
        ValueError: If the language is not in SUPPORTED_LANGUAGES.
    """
    supported_codes = {code for code, _ in SUPPORTED_LANGUAGES}
    if language not in supported_codes:
        raise ValueError(
            f"Unsupported language: {language!r}. Supported: {', '.join(sorted(supported_codes))}"
        )
    _current_language.set(language)


def deactivate() -> None:
    """Reset the current language to the default."""
    _current_language.set(DEFAULT_LANGUAGE)


def get_available_languages() -> list[tuple[str, str]]:
    """Return list of (code, name) tuples for all supported languages."""
    return list(SUPPORTED_LANGUAGES)


# ---------------------------------------------------------------------------
# Translation cache (LRU)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=128)
def _get_translation(
    domain: str,
    locales_dir: str,
    language: str,
) -> gettext_module.GNUTranslations | gettext_module.NullTranslations:
    """
    Get a cached translation object for a specific domain/locale/language.

    Args:
        domain: The translation domain (``'messages'`` for core, module_id for modules).
        locales_dir: Filesystem path to the locales directory (as string for hashability).
        language: ISO 639-1 language code.

    Returns:
        A GNUTranslations object if a .mo file is found, NullTranslations otherwise.
    """
    try:
        return gettext_module.translation(
            domain,
            localedir=locales_dir,
            languages=[language],
        )
    except FileNotFoundError:
        return gettext_module.NullTranslations()


def _clear_cache() -> None:
    """Clear the translation cache. Called when module locales change."""
    _get_translation.cache_clear()


# ---------------------------------------------------------------------------
# Translation functions
# ---------------------------------------------------------------------------


def _(text: str, module_id: str | None = None) -> str:
    """
    Translate a string using the current language.

    Fallback chain:
        1. If module_id provided, try module domain first
        2. Fallback to core domain (``messages``)
        3. Return original text

    Args:
        text: The source text in English.
        module_id: Optional module ID for module-specific translations.

    Returns:
        The translated string, or the original text if no translation found.
    """
    lang = _current_language.get()

    # English is the source language — no translation needed
    if lang == DEFAULT_LANGUAGE:
        return text

    # Try module-specific translation
    if module_id and module_id in _module_locales:
        t = _get_translation(module_id, str(_module_locales[module_id]), lang)
        result = t.gettext(text)
        if result != text:
            return result

    # Fallback to core translation
    t = _get_translation("messages", str(CORE_LOCALES_DIR), lang)
    return t.gettext(text)


def ngettext(
    singular: str,
    plural: str,
    n: int,
    module_id: str | None = None,
) -> str:
    """
    Pluralized translation.

    Args:
        singular: The singular form in English.
        plural: The plural form in English.
        n: The count determining plural form.
        module_id: Optional module ID for module-specific translations.

    Returns:
        The translated plural form.
    """
    lang = _current_language.get()

    if lang == DEFAULT_LANGUAGE:
        return singular if n == 1 else plural

    # Try module-specific translation
    if module_id and module_id in _module_locales:
        t = _get_translation(module_id, str(_module_locales[module_id]), lang)
        result = t.ngettext(singular, plural, n)
        if result not in (singular, plural):
            return result

    # Fallback to core translation
    t = _get_translation("messages", str(CORE_LOCALES_DIR), lang)
    return t.ngettext(singular, plural, n)


# ---------------------------------------------------------------------------
# Module locale registration
# ---------------------------------------------------------------------------


def register_module_locales(module_id: str, locales_dir: Path) -> None:
    """
    Register a module's locales directory.

    Called by ModuleLoader when a module with a ``locales/`` directory is loaded.

    Args:
        module_id: The module identifier.
        locales_dir: Path to the module's ``locales/`` directory.
    """
    if locales_dir.exists():
        _module_locales[module_id] = locales_dir
        _clear_cache()
        logger.debug("Registered locales for module %s: %s", module_id, locales_dir)


def unregister_module_locales(module_id: str) -> None:
    """
    Unregister a module's locales directory.

    Called by ModuleLoader when a module is unloaded.
    """
    if _module_locales.pop(module_id, None) is not None:
        _clear_cache()
        logger.debug("Unregistered locales for module %s", module_id)


def get_registered_module_locales() -> dict[str, Path]:
    """Return a copy of the registered module locales (for debugging/CLI)."""
    return dict(_module_locales)


# ---------------------------------------------------------------------------
# LazyString — deferred translation
# ---------------------------------------------------------------------------


class LazyString:
    """
    A string-like object that delays translation until ``str()`` is called.

    Useful for module-level constants that are evaluated at import time
    but should be translated at render time based on the current request language.

    Usage::

        MODULE_NAME = LazyString("Inventory", module_id="inventory")

        # Later, in a request context where language is set:
        str(MODULE_NAME)  # -> "Inventario" (if language is 'es')
        f"Module: {MODULE_NAME}"  # -> "Module: Inventario"
    """

    __slots__ = ("_module_id", "_text")

    def __init__(self, text: str, module_id: str | None = None) -> None:
        self._text = text
        self._module_id = module_id

    def __str__(self) -> str:
        return _(self._text, self._module_id)

    def __repr__(self) -> str:
        return f"LazyString({self._text!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, LazyString):
            return self._text == other._text
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._text)

    def __contains__(self, item: str) -> bool:
        return item in str(self)

    def __add__(self, other: str) -> str:
        return str(self) + other

    def __radd__(self, other: str) -> str:
        return other + str(self)

    def __len__(self) -> int:
        return len(str(self))

    def __bool__(self) -> bool:
        return bool(self._text)

    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)

    @property
    def source(self) -> str:
        """The original untranslated text."""
        return self._text


# ---------------------------------------------------------------------------
# Jinja2 translations adapter
# ---------------------------------------------------------------------------


class _RequestTranslations:
    """
    Adapter for ``jinja2.Environment.install_gettext_translations()``.

    Jinja2 expects an object with ``gettext()`` and ``ngettext()`` methods.
    This adapter delegates to the context-local language so that templates
    always render in the correct language for the current request.
    """

    def gettext(self, message: str) -> str:
        return _(message)

    def ngettext(self, singular: str, plural: str, n: int) -> str:
        return ngettext(singular, plural, n)

    def ugettext(self, message: str) -> str:
        return self.gettext(message)

    def ungettext(self, singular: str, plural: str, n: int) -> str:
        return self.ngettext(singular, plural, n)


def get_translations() -> _RequestTranslations:
    """
    Return a translations object compatible with Jinja2's i18n extension.

    The returned object uses the context-local language (set via ``activate()``)
    so templates are rendered in the correct language for each request.
    """
    return _RequestTranslations()
