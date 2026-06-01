"""Tests for the curated 6 recovery verbs (Phase 2 Item 4) in
hermes_cli.kanban_team_recovery: inspect / replay / requeue / mark_blocked /
restore_assignment / explain_blockage.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_store as kts
from plugins.agent_teams import kanban_team_recovery as recovery

# The ``conn`` fixture (conftest.py) lays down core schema then the plugin's
# team_* tables via kts.ensure_tables — these tables are no longer core schema.


def test_team_inspect_task_member_op(conn):
    rec = kt.team_create(conn, name="insp", goal="goal")
    kt.team_spawn(conn, rec.id, "alice", "researcher", "p-alice", "work")
    task_id = kt.team_task_create(conn, rec.id, "do it", owner="alice")

    by_task = recovery.inspect(conn, rec.id, task_id=task_id)
    assert by_task["task"]["task"]["id"] == task_id

    by_member = recovery.inspect(conn, rec.id, member="alice")
    assert by_member["member"]["profile"] == "p-alice"

    op_id = kts.op_begin(conn, operation_id=None, team_id=rec.id, verb="x", args={"a": 1})
    kts.op_finish(conn, op_id, status="failed", error="boom")
    by_op = recovery.inspect(conn, rec.id, operation_id=op_id)
    assert by_op["operation"]["status"] == "failed"


def test_team_replay_resets_failed_op_to_pending(conn):
    rec = kt.team_create(conn, name="rep", goal="goal")
    op_id = kts.op_begin(conn, operation_id=None, team_id=rec.id, verb="team_send", args={"k": 1})
    kts.op_finish(conn, op_id, status="failed", error="nope")
    out = recovery.replay(conn, op_id)
    assert out["operation"]["status"] == "pending"
    assert out["replayable"] is True


def test_team_requeue_clears_dead_letter(conn):
    rec = kt.team_create(conn, name="req", goal="goal")
    kt.team_spawn(conn, rec.id, "alice", "r", "p-alice", "work")
    kt.team_member_update(conn, rec.id, "alice", status="stopped")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="late")
    row = conn.execute(
        "SELECT id, dead_letter FROM team_messages WHERE team_id = ?", (rec.id,)
    ).fetchone()
    assert int(row["dead_letter"]) == 1
    out = recovery.requeue(conn, row["id"])
    assert int(out["dead_letter"]) == 0
    assert out["delivered_at"] is None


def test_team_mark_blocked(conn):
    rec = kt.team_create(conn, name="blk", goal="goal")
    task_id = kt.team_task_create(conn, rec.id, "work")  # no parents -> ready
    out = recovery.mark_blocked(conn, task_id, reason="waiting on spec")
    assert out["blocked"] is True
    assert kb.get_task(conn, task_id).status == "blocked"


def test_team_restore_assignment(conn):
    rec = kt.team_create(conn, name="restore", goal="goal")
    kt.team_spawn(conn, rec.id, "alice", "r", "p-alice", "work")
    task_id = kt.team_task_create(conn, rec.id, "orphan")  # unassigned
    # Simulate an orphaned task: strip the owner header + assignee.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body = 'just the prompt', assignee = NULL WHERE id = ?",
            (task_id,),
        )
    out = recovery.restore_assignment(conn, rec.id, task_id, member="alice")
    assert out["assignee"] == "p-alice"
    task = kb.get_task(conn, task_id)
    assert task.body.startswith(kt._owner_header("alice"))
    assert task.assignee == "p-alice"


def test_team_explain_blockage(conn):
    rec = kt.team_create(conn, name="why", goal="goal")
    parent = kt.team_task_create(conn, rec.id, "parent")
    child = kt.team_task_create(conn, rec.id, "child", dependencies=[parent])
    out = recovery.explain_blockage(conn, child)
    unmet_ids = {p["id"] for p in out["unmet_dependencies"]}
    assert parent in unmet_ids  # parent not done -> blocks child
