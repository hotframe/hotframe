<p align="center">
  <img src="https://raw.githubusercontent.com/hotframe/hotframe/main/logo.png" alt="hotframe" width="200">
</p>

<p align="center">
  <strong>Modular Python web framework with hot-mount dynamic modules and stateful, WebSocket-driven LiveComponents.</strong>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="License"></a>
  <a href="https://pypi.org/project/hotframe/"><img src="https://img.shields.io/pypi/v/hotframe.svg" alt="PyPI"></a>
</p>

<p align="center">
  <a href="https://hotframe.dev"><strong>hotframe.dev</strong></a>
</p>

---

## What hotframe is

hotframe is a Python web framework that combines FastAPI, SQLAlchemy 2.0, and Jinja2 under Django-like ergonomics. It adds two pieces:

- **A hot-mount module engine** — install, activate, deactivate, and uninstall plugins at runtime without restarting the process.
- **`LiveComponent`** — stateful, server-rendered components driven by a single WebSocket per page. Server holds the state; the client streams events and applies HTML patches with `morphdom`. No client-side framework, no build step.

## Install

```bash
pip install hotframe
```

## Quickstart

```bash
hf startproject myapp
cd myapp
hf runserver
```

```
INFO  hotframe.bootstrap   Application started in 142ms
INFO  uvicorn.error        Uvicorn running on http://127.0.0.1:8000
```

## A LiveComponent in 30 seconds

```python
# modules/todo/components/todo_list/component.py
from hotframe.live import LiveComponent, event
from modules.todo.models import Todo

class TodoList(LiveComponent):
    user_id: int                      # prop
    items: list = []                  # state
    new_text: str = ""

    async def on_mount(self) -> None:
        self.items = await Todo.where(user_id=self.user_id).all()

    @event("toggle")
    async def toggle(self, todo_id: str) -> None:
        t = next(t for t in self.items if str(t.id) == todo_id)
        t.done = not t.done
        await t.save()

    @event("add")
    async def add(self) -> None:
        if not self.new_text.strip():
            return
        await Todo.create(user_id=self.user_id, text=self.new_text)
        self.items = await Todo.where(user_id=self.user_id).all()
        self.new_text = ""
```

```jinja
{# modules/todo/components/todo_list/template.html #}
<ul>
{% for todo in items %}
  <li>
    <input type="checkbox" {% if todo.done %}checked{% endif %}
           data-on:click="toggle:{{ todo.id }}">
    {{ todo.text }}
  </li>
{% endfor %}
</ul>
<form data-on:submit="add">
  <input data-bind="new_text">
  <button type="submit">Add</button>
</form>
```

```jinja
{# any page template #}
{% extends "shared/base.html" %}
{% block body %}
  {% live "todo_list" user_id=user.id %}
{% endblock %}
```

That's it. Click the checkbox — the server toggles the todo, sends an HTML patch back, the DOM updates without a page reload, and the dev wrote zero JavaScript.

## Documentation

Full docs, guides and live examples at **[hotframe.dev](https://hotframe.dev)**.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
