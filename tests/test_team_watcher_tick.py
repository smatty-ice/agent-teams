"""End-to-end watcher-tick tests for the agent-teams plugin.

These port the still-relevant *integration* assertions from the old
``tests/gateway/test_team_notifier.py`` (which exercised the deleted core
``gateway/team_notifier._team_tick`` coroutine). The auto-idle + push behavior
now lives in ``plugins.agent_teams.watcher._tick_all_boards``, which drives the
same ``run_auto_idle_pass`` / ``run_push_delivery_pass`` passes over every board
on disk.

Unlike ``test_watcher.py`` (which mocks the passes to verify the per-board
plumbing), this drives REAL DB state through ``_tick_all_boards`` and asserts
the roster/task transitions actually happen — the substance of the dropped
notifier tests.

DROPPED from the old file (cannot survive the repackage):
  * ``test_team_notifier_imports_and_hook_exists`` — imported ``gateway.run`` /
    ``gateway.team_notifier`` and asserted ``GatewayRunner._team_watcher`` +
    ``team_notifier.team_watcher_loop``. The core gateway coroutine and its
    ``gateway/run.py`` wiring no longer exist in the plugin; the watcher is now
    a ``pre_gateway_dispatch`` hook (see test_watcher.py / test_plugin_load.py).
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import watcher


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fresh isolated default board under tmp_path (mirrors the old
    notifier ``board`` fixture)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    try:
        kb._INITIALIZED_PATHS.clear()
    except AttributeError:
        pass
    kb.init_db()
    return tmp_path


def test_tick_inert_with_no_teams(home):
    """A board with no teams produces a tick that does nothing and raises
    nothing (ported from test_team_tick_inert_with_no_teams)."""
    # Must not raise; with no teams every board is skipped.
    watcher._tick_all_boards()


def test_tick_runs_auto_idle_and_push(home):
    """``_tick_all_boards`` flips a completed member to idle (auto-idle) and
    re-readies a separately-messaged idle member (push) in one pass.

    Ported from test_team_tick_runs_auto_idle_and_push — same scenario, now
    driven through the plugin watcher instead of the deleted core notifier.
    """
    conn = kb.connect()
    try:
        from plugins.agent_teams import kanban_team_store as kts
        kts.ensure_tables(conn)
        team = kt.team_create(conn, name="alpha", goal="ship it")
        # Member A: drive a real completed run → auto-idle should mark idle.
        a = kt.team_spawn(conn, team.id, "aye", "teammate", "profile-a", "work")
        kb.claim_task(conn, a.task_id)
        kb.complete_task(conn, a.task_id, result="done")

        # Member B: idle, with a freshly-sent message → push should wake it.
        b = kt.team_spawn(conn, team.id, "bee", "teammate", "profile-b", "work")
        kt.team_member_update(conn, team.id, "bee", status="idle")
        # Park B's task in todo so wake_member has a non-running task to re-ready.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (b.task_id,))
        kt.team_send(conn, team.id, to="bee", from_sender="lead", message="go")
        team_id = team.id
        b_task = b.task_id
    finally:
        conn.close()

    # One watcher tick runs both passes across every board on disk.
    watcher._tick_all_boards()

    conn = kb.connect()
    try:
        reloaded = kt.get_team(conn, team_id)
        # Auto-idle flipped the completed member.
        assert reloaded.members["aye"].status == "idle"
        # Push re-readied B's task.
        assert kb.get_task(conn, b_task).status == "ready"
    finally:
        conn.close()
