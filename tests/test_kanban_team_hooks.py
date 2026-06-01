"""Tests for hermes_cli.kanban_team_hooks — the hook registry + event pump
(Phase 2 Item 7).

Net-new plumbing over task_events (the gateway's unseen_events_for_sub is the
design precedent). Hook kinds map to Kanban event kinds and are filtered to the
team's tenant.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_hooks as hooks

# The ``conn`` fixture (conftest.py) lays down core schema then the plugin's
# team_* tables via ensure_tables — these tables are no longer core schema.


def test_team_hook_subs_table_exists_after_init(conn):
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "team_hook_subs" in tables


def test_member_idle_emits_team_idle_event(conn):
    """team_member_update(status='idle') must emit a team_idle event on the
    team root (TeammateIdle has no native Kanban-event source)."""
    rec = kt.team_create(conn, name="idleteam", goal="goal")
    kt.team_spawn(conn, rec.id, "alice", "r", "p-alice", "work")
    kt.team_member_update(conn, rec.id, "alice", status="idle")
    kinds = [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (rec.id,)
        ).fetchall()
    ]
    assert "team_idle" in kinds


def test_poll_hook_taskcreated_filters_to_team(conn):
    """A TaskCreated hook only sees `created` events for tasks in this team's
    tenant — not unrelated tasks on the same board."""
    rec = kt.team_create(conn, name="tcteam", goal="goal")
    sub_id = hooks.register_hook(conn, team_id=rec.id, hook_kind="TaskCreated")

    # A team work task (tenant=team:tcteam) — should be seen.
    team_task = kt.team_task_create(conn, rec.id, "team work")
    # An unrelated task on the same board — should NOT be seen.
    kb.create_task(conn, title="unrelated", tenant=None)

    new_cursor, events = hooks.poll_hook(conn, sub_id)
    seen_task_ids = {e.task_id for e in events}
    assert team_task in seen_task_ids
    assert all(e.kind == "created" for e in events)
    # The unrelated task id is not in the team-scoped feed.
    unrelated = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE title = 'unrelated'"
        ).fetchall()
    ]
    assert unrelated and unrelated[0] not in seen_task_ids


def test_poll_hook_taskcompleted_and_ack_advances_cursor(conn):
    rec = kt.team_create(conn, name="compteam", goal="goal")
    sub_id = hooks.register_hook(conn, team_id=rec.id, hook_kind="TaskCompleted")
    task_id = kt.team_task_create(conn, rec.id, "finish me")
    # complete_task accepts a ready task directly and fires `completed`.
    kb.complete_task(conn, task_id, result="done")

    new_cursor, events = hooks.poll_hook(conn, sub_id)
    assert any(e.kind == "completed" and e.task_id == task_id for e in events)
    assert new_cursor > 0

    hooks.ack_hook(conn, sub_id, new_cursor)
    # After ack, the same events are not re-delivered.
    _, again = hooks.poll_hook(conn, sub_id)
    assert again == []


def test_poll_hook_teammateidle(conn):
    rec = kt.team_create(conn, name="idlehook", goal="goal")
    sub_id = hooks.register_hook(conn, team_id=rec.id, hook_kind="TeammateIdle")
    kt.team_spawn(conn, rec.id, "alice", "r", "p-alice", "work")
    kt.team_member_update(conn, rec.id, "alice", status="idle")

    _, events = hooks.poll_hook(conn, sub_id)
    assert any(e.kind == "team_idle" for e in events)
