"""Global test configuration."""

import pytest

from hotframe.config.settings import reset_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """Ensure clean settings state between tests."""
    reset_settings()
    yield
    reset_settings()
