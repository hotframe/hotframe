"""Tests for hotframe.components.rendering and jinja_ext."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from jinja2 import DictLoader, Environment, FileSystemLoader
from markupsafe import Markup

from hotframe.components import ComponentRegistry
from hotframe.components.discovery import discover_components
from hotframe.components.jinja_ext import (
    ComponentExtension,
    install_component_context_tracker,
)
from hotframe.components.rendering import register_component_globals

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_root():
    tmp = Path(tempfile.mkdtemp(prefix="hotframe-components-render-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_env_with_registry(
    search_dirs: list[Path],
    registry: ComponentRegistry,
) -> Environment:
    env = Environment(
        loader=FileSystemLoader([str(p) for p in search_dirs]),
        extensions=[ComponentExtension],
        autoescape=True,
    )
    install_component_context_tracker(env)
    register_component_globals(env)
    env.globals["_hotframe_components"] = registry
    return env


def _write_template(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRenderComponentGlobal:
    def test_template_only_component(self, tmp_root):
        _write_template(tmp_root, "badge/template.html", "<span>{{ text }}</span>")
        registry = ComponentRegistry()
        entries = discover_components(tmp_root, import_prefix="_hf_render_template_only")
        for e in entries:
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('badge', text='hello') }}")
        result = tpl.render()
        assert "<span>hello</span>" in result
        assert isinstance(Markup(result), Markup)

    def test_unknown_component_logs_and_returns_empty(self, tmp_root, caplog):
        registry = ComponentRegistry()
        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("X{{ render_component('nope') }}Y")
        with caplog.at_level("WARNING"):
            result = tpl.render()
        assert result == "XY"
        assert any("Unknown component" in rec.message for rec in caplog.records)

    def test_python_component_validates_props(self, tmp_root):
        comp = tmp_root / "card"
        comp.mkdir()
        (comp / "template.html").write_text("<div>{{ title }}|{{ subtitle }}</div>")
        (comp / "component.py").write_text(
            "from hotframe.components import Component\n\n"
            "class Card(Component):\n"
            "    title: str\n"
            "    subtitle: str = 'x'\n"
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_render_python"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)

        # Happy path.
        tpl = env.from_string("{{ render_component('card', title='T') }}")
        result = tpl.render()
        assert "<div>T|x</div>" in result

    def test_python_component_missing_required_prop_logs_warning(self, tmp_root, caplog):
        comp = tmp_root / "card"
        comp.mkdir()
        (comp / "template.html").write_text("<div>{{ title }}</div>")
        (comp / "component.py").write_text(
            "from hotframe.components import Component\n\nclass Card(Component):\n    title: str\n"
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_render_missing"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('card') }}")
        with caplog.at_level("WARNING"):
            result = tpl.render()
        assert "invalid props" in result
        assert any("prop validation failed" in rec.message for rec in caplog.records)

    def test_framework_slice_is_injected(self, tmp_root):
        _write_template(
            tmp_root,
            "probe/template.html",
            "csrf={{ csrf_token }} nonce={{ csp_nonce }}",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_render_slice"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('probe') }}")
        result = tpl.render(csrf_token="tok", csp_nonce="N1", leaked="should-not-appear")
        assert "csrf=tok" in result
        assert "nonce=N1" in result

    def test_isolation_parent_vars_do_not_leak(self, tmp_root):
        _write_template(
            tmp_root,
            "leak/template.html",
            "[{{ secret | default('NOPE') }}]",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_render_iso"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('leak') }}")
        # ``secret`` is a parent-scope variable; isolation means the
        # component template must NOT see it.
        result = tpl.render(secret="leaked!")
        assert "[NOPE]" in result
        assert "leaked" not in result


class TestComponentTag:
    def test_body_is_exposed(self, tmp_root):
        _write_template(
            tmp_root,
            "modal/template.html",
            "<div class='modal'>{{ title }}::{{ body }}</div>",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_tag_body"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{% component 'modal' title='Hi' %}<p>Sure?</p>{% endcomponent %}")
        result = tpl.render()
        assert "<div class='modal'>Hi::<p>Sure?</p></div>" in result

    def test_attrs_kwarg_carries_reserved_html_attrs(self, tmp_root):
        # Use ``attrs`` dict to pass HTML attributes whose names would
        # collide with Python keywords.
        _write_template(
            tmp_root,
            "btn/template.html",
            "<button class=\"{{ attrs['class'] }}\" id=\"{{ attrs['id'] }}\">{{ body }}</button>",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_tag_attrs"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string(
            "{% component 'btn' attrs={'class': 'btn-primary', 'id': 'save'} %}Save{% endcomponent %}"
        )
        result = tpl.render()
        assert 'class="btn-primary"' in result
        assert 'id="save"' in result
        assert ">Save</button>" in result

    def test_framework_slice_reaches_tag_body(self, tmp_root):
        _write_template(
            tmp_root,
            "probe/template.html",
            "<p>csrf={{ csrf_token }}</p>{{ body }}",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_tag_slice"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{% component 'probe' %}inner{% endcomponent %}")
        result = tpl.render(csrf_token="tok")
        assert "csrf=tok" in result
        assert "inner" in result


class TestAlertAndBadgeLocal:
    """Regression tests equivalent to the removed built-in suite.

    Alert and badge are no longer shipped with the framework; the scaffold
    generated by ``hf startproject`` creates them in the new project.
    These tests recreate equivalent templates on disk and exercise the
    same rendering surface.
    """

    def test_alert_renders_with_body(self, tmp_root):
        _write_template(
            tmp_root,
            "alert/template.html",
            '<div class="alert alert-{{ type | default(\'info\') }}" role="alert">{{ body }}</div>',
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_local_alert"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{% component 'alert' type='warning' %}Careful!{% endcomponent %}")
        result = tpl.render()
        assert 'class="alert alert-warning"' in result
        assert "Careful!" in result

    def test_badge_renders_via_global(self, tmp_root):
        _write_template(
            tmp_root,
            "badge/template.html",
            "<span class=\"badge badge-{{ variant | default('default') }}\">{{ text }}</span>",
        )
        registry = ComponentRegistry()
        for e in discover_components(tmp_root, import_prefix="_hf_local_badge"):
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('badge', text='New', variant='primary') }}")
        result = tpl.render()
        assert 'class="badge badge-primary"' in result
        assert "New" in result


class TestNameCollision:
    """Regression: user props named ``name`` must not collide with dispatch."""

    def test_render_component_accepts_name_prop(self, tmp_root):
        # Component template uses a ``name`` prop directly.
        _write_template(tmp_root, "user/template.html", "<span>{{ name }}</span>")
        registry = ComponentRegistry()
        entries = discover_components(tmp_root, import_prefix="_hf_test_collision_global")
        for e in entries:
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{{ render_component('user', name='Ioan') }}")
        result = tpl.render()
        assert "<span>Ioan</span>" in result

    def test_component_tag_accepts_name_prop(self, tmp_root):
        _write_template(
            tmp_root,
            "user_badge/template.html",
            "<span>{{ name }}</span>{% if body %}<i>{{ body }}</i>{% endif %}",
        )
        registry = ComponentRegistry()
        entries = discover_components(tmp_root, import_prefix="_hf_test_collision_tag")
        for e in entries:
            registry.register(e)

        env = _make_env_with_registry([tmp_root], registry)
        tpl = env.from_string("{% component 'user_badge' name='Ioan' %}admin{% endcomponent %}")
        result = tpl.render()
        assert "<span>Ioan</span>" in result
        assert "<i>admin</i>" in result


class TestNoRegistryInjected:
    def test_render_component_without_registry_returns_empty(self, tmp_root, caplog):
        env = Environment(
            loader=DictLoader({"t.html": "{{ render_component('x') }}"}),
            extensions=[ComponentExtension],
            autoescape=True,
        )
        install_component_context_tracker(env)
        register_component_globals(env)
        # No _hotframe_components binding.
        with caplog.at_level("WARNING"):
            result = env.get_template("t.html").render()
        assert result == ""
        assert any("ComponentRegistry was bound" in rec.message for rec in caplog.records)
