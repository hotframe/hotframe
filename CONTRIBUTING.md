# Contributing to hotframe

Thanks for your interest. **hotframe** is open source under Apache 2.0.

## Reporting issues

Search the [existing issues](https://github.com/hotframe/hotframe/issues)
first. If you can't find a match, open a new one with:

- What you expected to happen.
- What actually happened.
- Steps to reproduce (a minimal `hf startproject` snippet is gold).
- Python version + OS.

## Pull requests

Small focused PRs are welcome. Before opening one:

1. Run the test suite: `pytest -q`.
2. Run the linter: `ruff check src tests`.
3. Run type checks: `mypy src/hotframe`.

If you're touching the **live runtime** (`hotframe.live`) or the
**module engine** (`hotframe.engine`), add or update tests under
`src/hotframe/<package>/tests/` so the change is exercised.

## Commits

Conventional Commits style. Subject line in imperative mood, ≤ 72 chars:

```
feat(live): support binary payloads on event handlers
fix(engine): clean sys.modules entries on module unload
docs: clarify settings.py boot sequence
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`,
`perf`, `build`, `ci`.

## Code of Conduct

By participating in this project you agree to abide by its
[Code of Conduct](CODE_OF_CONDUCT.md).
