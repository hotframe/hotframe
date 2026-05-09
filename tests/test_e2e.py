"""End-to-end tests — create app, make requests, verify responses."""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from hotframe.config.settings import HotframeSettings, reset_settings, set_settings
from hotframe.management.cli import app as cli_app

runner = CliRunner()


class TestAppBootstrap:
    """Test that a fresh hotframe app boots and serves requests."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        reset_settings()
        settings = HotframeSettings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            DEBUG=True,
            LOG_LEVEL="WARNING",
        )
        set_settings(settings)

        from hotframe.bootstrap import create_app

        app = create_app(settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_api_docs_available_in_debug(self):
        reset_settings()
        settings = HotframeSettings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            DEBUG=True,
            LOG_LEVEL="WARNING",
        )
        set_settings(settings)

        from hotframe.bootstrap import create_app

        app = create_app(settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/docs")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_404_for_unknown_route(self):
        reset_settings()
        settings = HotframeSettings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            DEBUG=True,
            LOG_LEVEL="WARNING",
        )
        set_settings(settings)

        from hotframe.bootstrap import create_app

        app = create_app(settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/nonexistent")
            assert response.status_code == 404


class TestStartprojectE2E:
    """Test the full scaffolding flow."""

    def test_startproject_creates_valid_structure(self, tmp_path):
        os.chdir(tmp_path)
        result = runner.invoke(cli_app, ["startproject", "myapp"])
        assert result.exit_code == 0

        project = tmp_path / "myapp"
        assert (project / "main.py").exists()
        assert (project / "settings.py").exists()
        assert (project / "asgi.py").exists()
        assert (project / "manage.py").exists()
        assert (project / "pyproject.toml").exists()
        assert (project / ".env").exists()
        assert (project / ".gitignore").exists()
        assert (project / "apps" / "__init__.py").exists()
        assert (project / "modules").is_dir()
        assert (project / "tests" / "__init__.py").exists()
        assert (project / "tests" / "conftest.py").exists()

        # Verify settings.py is valid Python
        settings_content = (project / "settings.py").read_text()
        assert "HotframeSettings" in settings_content
        assert "MYAPP_" in settings_content  # env_prefix
