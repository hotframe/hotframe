# Changelog

All notable changes to **hotframe** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-05-10

First public release under the new `hotframe.dev` project home.

### Added

- **`hotframe.live`** — stateful, server-rendered, WebSocket-driven
  components. `LiveComponent` base class, `@event` decorator,
  `LiveSession`, `LiveRuntime`, `/ws/_live` endpoint, and the
  vendored `live.js` + `morphdom` client (~12 KB minified+gzip).
- **`{% live %}` Jinja2 tag** for cold-loading live components from
  any template.
- **`{{ live_assets() }}` Jinja2 global** that emits the script tags
  for the live runtime client.
- **JinjaX integration** for component-style template syntax.
- **Hot-mount module engine** (`ModuleRuntime`) — install, activate,
  deactivate, uninstall, and update modules at runtime.
- **`@view` decorator** with auth + permission gating + template
  auto-discovery.
- **`Component` / `ComponentRegistry`** for stateless reusable
  widgets, discovered from `apps/*/components/` and
  `modules/*/components/`.
- **Slot system** (`SlotRegistry`) for cross-module UI injection.
- **Event bus** (`AsyncEventBus`), **hooks** (`HookRegistry`), and
  **typed events** (`BaseEvent` + `@register_event`).
- **CRUD repository pattern** (`BaseRepository`) and abstract
  persistence protocols (`ISession`, `IQueryBuilder`, `IRepository`).
- **CSRF**, **CSP**, **rate limiting**, **CORS**, and **session**
  middleware out of the box.
- **CLI** (`hf`) with `startproject`, `startapp`, `startmodule`,
  `runserver`, `migrate`, `makemigrations`, `shell`, and module
  lifecycle commands.

### Project notes

- This `1.0.0` line is a clean reset. Earlier `0.x` releases on PyPI
  shipped a different reactivity layer and are not considered
  API-stable predecessors.
- Repository moved to [github.com/hotframe/hotframe](https://github.com/hotframe/hotframe).
- License: Apache 2.0.
