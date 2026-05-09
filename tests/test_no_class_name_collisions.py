# SPDX-License-Identifier: Apache-2.0
"""
Cross-module guard: no two SQLAlchemy mappers on the shared declarative
``Base`` may share a class name.

When two modules declare classes with the same name on the shared
declarative base (for example a generic ``Thread`` in both ``assistant``
and ``communications``), SQLAlchemy's ``relationship()`` resolution by
bare class name becomes ambiguous and raises
``InvalidRequestError: Multiple classes found for path "<name>"``. Once
that happens the global ORM is poisoned until the process restarts.

This test fails as soon as a duplicate is introduced — it does not
matter which modules declare the classes, the test surfaces the offence
by name.

The test imports ``hotframe.models.base.Base`` and counts the
``__name__`` values across every mapper currently registered. When this
file runs in the hotframe test suite alone the registry is small. The
real value comes from running the full hub test suite (which loads every
project + module model) — but the assertion is symmetric: a single
duplicate, anywhere, fails the test.
"""

from __future__ import annotations

import collections

from hotframe.models.base import Base


def test_no_class_name_collisions_on_shared_base() -> None:
    counts = collections.Counter(mapper.class_.__name__ for mapper in Base.registry.mappers)
    duplicates = {name: count for name, count in counts.items() if count > 1}
    assert not duplicates, (
        "Class name collisions on shared SQLAlchemy Base — relationship() "
        "by bare class name will be ambiguous and the global ORM will be "
        f"poisoned: {duplicates}"
    )
