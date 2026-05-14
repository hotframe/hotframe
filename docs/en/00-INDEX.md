# 00. Study index — hotframe from the inside

This folder documents **the entire source code** of `src/hotframe/`, one
document per folder. The goal: anyone who reads all 21 documents in
order should reach a maintainer-level understanding of hotframe, not
just a user-level one.

The starting point remains [GUIDE.md](GUIDE.md) — the "from the outside"
usage guide. These documents are the "from the inside" view: how each
subsystem is implemented.

## How to use this index

We will go through the documents one by one, in the order below (the
sequence is designed as a learning progression: from the foundations to
the pieces that build on them):

1. You read the document for that row.
2. You explain it back to me / ask about anything that wasn't clear.
3. Once you confirm you've understood it, we check its box `[x]` and
   move on to the next one.

## Reading list

### Foundations — configuration and discovery

- [ ] **01** · [Configuration: settings, paths, and database](01-config.md) — `config/`
- [ ] **02** · [Convention-based auto-discovery](02-discovery.md) — `discovery/`
- [ ] **03** · [Static apps](03-apps.md) — `apps/`

### Persistence

- [ ] **04** · [Models and querysets](04-models.md) — `models/`
- [ ] **05** · [ORM events and transactions](05-orm.md) — `orm/`
- [ ] **06** · [Database protocols](06-db.md) — `db/`
- [ ] **07** · [The generic repository](07-repository.md) — `repository/`
- [ ] **08** · [Migrations](08-migrations.md) — `migrations/`

### Presentation layer

- [ ] **09** · [The template engine](09-templating.md) — `templating/`
- [ ] **10** · [Stateless components](10-components.md) — `components/`
- [ ] **11** · [The reactive runtime](11-live.md) — `live/`
- [ ] **12** · [Views and responses](12-views.md) — `views/`

### Communication and messaging

- [ ] **13** · [HTTP client and interceptors](13-http.md) — `http/`
- [ ] **14** · [Events, hooks, and signals](14-signals.md) — `signals/`

### HTTP stack and security

- [ ] **15** · [The middleware stack](15-middleware.md) — `middleware/`
- [ ] **16** · [Security and authentication](16-auth.md) — `auth/`

### The module engine

- [ ] **17** · [The module engine](17-engine.md) — `engine/` — *the longest document and the heart of the framework*

### Tooling and operations

- [ ] **18** · [The CLI](18-management.md) — `management/`
- [ ] **19** · [Auto-reload in development](19-dev.md) — `dev/`
- [ ] **20** · [Observability](20-utils.md) — `utils/`
- [ ] **21** · [Testing utilities](21-testing.md) — `testing/`

## Folder → document map

| Folder in `src/hotframe/` | Document |
|---|---|
| `config/` | [01-config.md](01-config.md) |
| `discovery/` | [02-discovery.md](02-discovery.md) |
| `apps/` | [03-apps.md](03-apps.md) |
| `models/` | [04-models.md](04-models.md) |
| `orm/` | [05-orm.md](05-orm.md) |
| `db/` | [06-db.md](06-db.md) |
| `repository/` | [07-repository.md](07-repository.md) |
| `migrations/` | [08-migrations.md](08-migrations.md) |
| `templating/` | [09-templating.md](09-templating.md) |
| `components/` | [10-components.md](10-components.md) |
| `live/` | [11-live.md](11-live.md) |
| `views/` | [12-views.md](12-views.md) |
| `http/` | [13-http.md](13-http.md) |
| `signals/` | [14-signals.md](14-signals.md) |
| `middleware/` | [15-middleware.md](15-middleware.md) |
| `auth/` | [16-auth.md](16-auth.md) |
| `engine/` | [17-engine.md](17-engine.md) |
| `management/` | [18-management.md](18-management.md) |
| `dev/` | [19-dev.md](19-dev.md) |
| `utils/` | [20-utils.md](20-utils.md) |
| `testing/` | [21-testing.md](21-testing.md) |

> The standalone files at the root (`bootstrap.py`, `asgi.py`,
> `__init__.py`) do not have their own document: `bootstrap.py` is
> covered cross-sectionally in almost all of them (it is the piece that
> assembles everything in `create_app`), and is referenced in each
> "How this fits into the rest of the framework" section.

## Progress

`0 / 21` documents completed.
