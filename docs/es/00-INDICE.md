# 00. Índice de estudio — hotframe por dentro

Esta carpeta documenta **todo el código fuente** de `src/hotframe/`, una
carpeta por documento. El objetivo: que quien lea los 21 documentos en
orden llegue a entender hotframe a nivel de mantenedor del framework, no
solo de usuario.

El punto de partida sigue siendo [GUIDE.md](GUIDE.md) — la guía de uso
"desde fuera". Estos documentos son la vista "desde dentro": cómo está
implementado cada subsistema.

## Cómo usar este índice

Vamos a ir documento por documento, en el orden de abajo (está pensado
como una progresión de aprendizaje: de los cimientos a las piezas que se
apoyan en ellos):

1. Tú lees el documento de la fila.
2. Me lo explicas / me preguntas lo que no haya quedado claro.
3. Cuando confirmes que lo has entendido, marcamos su casilla `[x]` y
   pasamos al siguiente.

## Lista de lectura

### Cimientos — configuración y descubrimiento

- [ ] **01** · [Configuración: settings, paths y base de datos](01-config.md) — `config/`
- [ ] **02** · [Auto-descubrimiento por convención](02-discovery.md) — `discovery/`
- [ ] **03** · [Apps estáticas](03-apps.md) — `apps/`

### Persistencia

- [ ] **04** · [Modelos y queryset](04-models.md) — `models/`
- [ ] **05** · [Eventos del ORM y transacciones](05-orm.md) — `orm/`
- [ ] **06** · [Protocolos de base de datos](06-db.md) — `db/`
- [ ] **07** · [El repositorio genérico](07-repository.md) — `repository/`
- [ ] **08** · [Migraciones](08-migrations.md) — `migrations/`

### Capa de presentación

- [ ] **09** · [El motor de plantillas](09-templating.md) — `templating/`
- [ ] **10** · [Componentes stateless](10-components.md) — `components/`
- [ ] **11** · [El runtime reactivo](11-live.md) — `live/`
- [ ] **12** · [Vistas y respuestas](12-views.md) — `views/`

### Comunicación y mensajería

- [ ] **13** · [Cliente HTTP e interceptores](13-http.md) — `http/`
- [ ] **14** · [Eventos, hooks y señales](14-signals.md) — `signals/`

### Pila HTTP y seguridad

- [ ] **15** · [La pila de middleware](15-middleware.md) — `middleware/`
- [ ] **16** · [Seguridad y autenticación](16-auth.md) — `auth/`

### El motor de módulos

- [ ] **17** · [El motor de módulos](17-engine.md) — `engine/` — *el documento más largo y el corazón del framework*

### Tooling y operación

- [ ] **18** · [El CLI](18-management.md) — `management/`
- [ ] **19** · [Autoreload en desarrollo](19-dev.md) — `dev/`
- [ ] **20** · [Observabilidad](20-utils.md) — `utils/`
- [ ] **21** · [Utilidades de testing](21-testing.md) — `testing/`

## Mapa carpeta → documento

| Carpeta de `src/hotframe/` | Documento |
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

> Los archivos sueltos de la raíz (`bootstrap.py`, `asgi.py`,
> `__init__.py`) no tienen documento propio: `bootstrap.py` se explica de
> forma transversal en casi todos (es quien ensambla todo en
> `create_app`), y se referencia en cada "Cómo encaja con el resto del
> framework".

## Progreso

`0 / 21` documentos completados.
