"""Test fixtures for the agent-teams plugin.

The plugin directory is ``plugins/agent-teams`` (hyphenated), which is **not** a
valid Python identifier, so ``from plugins.agent_teams import ...`` cannot resolve
through normal import machinery. The real Hermes plugin loader sidesteps this by
loading the package via ``importlib.util.spec_from_file_location`` under a slug
where ``-`` is replaced with ``_`` (see ``hermes_cli/plugins.py``: ``slug =
key.replace("/", "__").replace("-", "_")``).

We mirror that here: register the hyphen-dir package in ``sys.modules`` as
``plugins.agent_teams`` so test modules can use the canonical underscore import.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

import plugins  # real package: plugins/__init__.py exists

_PLUGIN_DIR = Path(plugins.__file__).resolve().parent / "agent-teams"
_PKG_NAME = "plugins.agent_teams"


def _ensure_package() -> types.ModuleType:
    """Idempotently expose plugins/agent-teams as the importable package
    ``plugins.agent_teams`` (underscore), so relative imports inside the
    moved modules (``from . import kanban_team_store``) resolve **and** the
    real ``__init__.py`` (with ``register``) is executed.

    Mirrors the real Hermes plugin loader (``hermes_cli/plugins.py``:
    ``spec_from_file_location(..., submodule_search_locations=[plugin_dir])``
    + ``exec_module``) so ``from plugins.agent_teams import register`` works
    under pytest exactly as it does under the live loader. We load from the
    real ``__init__.py`` rather than faking an empty namespace package so the
    package's public API is genuinely importable."""
    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME]
    init_file = _PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        init_file,
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = _PKG_NAME
    pkg.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG_NAME] = pkg
    setattr(plugins, "agent_teams", pkg)
    spec.loader.exec_module(pkg)
    return pkg


_ensure_package()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """A fresh Kanban connection in an isolated HERMES_HOME, with the
    plugin's tables ensured (they are no longer part of core schema).

    Shared by every ported team test module (test_kanban_team*.py,
    test_team_tools.py): the plan calls for one consolidated ``conn`` fixture
    that lays down core schema via ``kb.init_db()`` then the plugin's three
    ``team_*`` tables via ``kts.ensure_tables(c)``. Mirrors the original
    per-module fixtures (clear the init-cache + mkdir the home) so collapsing
    them into conftest is behavior-preserving."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    from hermes_cli import kanban_db as kb
    from plugins.agent_teams import kanban_team_store as kts

    # init_db is idempotent but caches "already initialized" paths; clear so
    # each test's tmp_path actually gets the schema laid down.
    try:
        kb._INITIALIZED_PATHS.clear()
    except AttributeError:
        pass
    kb.init_db()
    c = kb.connect()
    # The team_* tables are no longer part of core schema; the plugin creates
    # them idempotently. Guard for the brief TDD window before ensure_tables
    # lands (Task 4) so list_comments_after (Task 3, on the core task_comments
    # table) can be verified independently.
    if hasattr(kts, "ensure_tables"):
        kts.ensure_tables(c)
    try:
        yield c
    finally:
        c.close()
