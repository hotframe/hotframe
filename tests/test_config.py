"""Tests for hotframe.config.settings."""

import pytest

from hotframe.config.settings import HotframeSettings, get_settings, reset_settings, set_settings


@pytest.fixture(autouse=True)
def _reset():
    """Reset settings singleton between tests."""
    reset_settings()
    yield
    reset_settings()


class TestHotframeSettings:
    def test_defaults(self):
        s = HotframeSettings()
        assert s.DEBUG is True
        assert s.DEPLOYMENT_MODE == "local"
        assert s.LANGUAGE == "en"
        assert s.CURRENCY == "USD"
        assert s.LOG_LEVEL == "INFO"
        assert s.SESSION_COOKIE_NAME == "session"
        assert s.APP_TITLE == "Hotframe App"

    def test_is_sqlite(self):
        s = HotframeSettings()
        assert s.is_sqlite is True

    def test_is_production_false_in_local(self):
        s = HotframeSettings()
        assert s.is_production is False

    def test_is_production_true(self):
        s = HotframeSettings(
            DEPLOYMENT_MODE="web",
            DEBUG=False,
            SECRETS_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        )
        assert s.is_production is True

    def test_log_level_normalization(self):
        s = HotframeSettings(LOG_LEVEL="debug")
        assert s.LOG_LEVEL == "DEBUG"

    def test_invalid_log_level(self):
        with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
            HotframeSettings(LOG_LEVEL="invalid")

    def test_middleware_defaults(self):
        s = HotframeSettings()
        assert len(s.MIDDLEWARE) > 0
        assert "hotframe.auth.csrf.CSRFMiddleware" in s.MIDDLEWARE

    def test_csrf_exempt_defaults(self):
        s = HotframeSettings()
        assert "/api/" in s.CSRF_EXEMPT_PREFIXES
        assert "/health" in s.CSRF_EXEMPT_PREFIXES


class TestSettingsSingleton:
    def test_get_settings_returns_same(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_set_settings(self):
        custom = HotframeSettings(APP_TITLE="Custom")
        set_settings(custom)
        assert get_settings().APP_TITLE == "Custom"

    def test_reset_settings(self):
        set_settings(HotframeSettings(APP_TITLE="Custom"))
        reset_settings()
        s = get_settings()
        assert s.APP_TITLE == "Hotframe App"
