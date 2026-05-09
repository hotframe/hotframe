# SPDX-License-Identifier: Apache-2.0
"""Tests for the inner-render + envelope helpers."""

from __future__ import annotations

import json

from jinja2 import DictLoader, Environment

from hotframe.components.entry import ComponentEntry
from hotframe.live import LiveComponent
from hotframe.live.diff import render_component_inner, wrap_with_envelope


class Sample(LiveComponent):
    name: str = "world"
    count: int = 0


def _env() -> Environment:
    return Environment(
        loader=DictLoader({"sample/template.html": "<p>Hello {{ name }} ({{ count }})</p>"})
    )


def test_render_component_inner_substitutes_state() -> None:
    env = _env()
    entry = ComponentEntry(
        name="sample", template="sample/template.html", props_cls=Sample, is_live=True
    )
    inst = Sample(name="ioan", count=3)
    out = render_component_inner(env, entry, inst)
    assert out == "<p>Hello ioan (3)</p>"


def test_wrap_with_envelope_emits_data_attrs() -> None:
    inst = Sample(name="ioan", count=0)
    inst._cid = "c-abc"
    inst._component_name = "sample"

    html = wrap_with_envelope(
        "<p>x</p>",
        cid="c-abc",
        component_name="sample",
        instance=inst,
        prop_names=["name", "count"],
    )

    assert 'data-hf-cid="c-abc"' in html
    assert 'data-hf-component="sample"' in html
    assert 'data-hf-props="' in html
    assert html.startswith("<div ")
    assert html.endswith("</div>")
    assert "<p>x</p>" in html


def test_wrap_with_envelope_props_are_html_escaped_json() -> None:
    inst = Sample(name='evil"<x>', count=1)
    html = wrap_with_envelope(
        "",
        cid="c-1",
        component_name="sample",
        instance=inst,
        prop_names=["name", "count"],
    )

    # The double quotes in the JSON must be escaped as &quot; for the
    # attribute to be valid HTML. Brackets must also be escaped.
    assert "&quot;" in html
    assert "&lt;x&gt;" in html

    # The decoded JSON should still be valid round-trip — extract via a
    # quick & dirty parse: the props attribute starts with data-hf-props="
    start = html.index('data-hf-props="') + len('data-hf-props="')
    end = html.index('"', start)
    props_attr = html[start:end]
    # html.unescape to read it back.
    import html as html_mod

    decoded = html_mod.unescape(props_attr)
    parsed = json.loads(decoded)
    assert parsed == {"name": 'evil"<x>', "count": 1}
