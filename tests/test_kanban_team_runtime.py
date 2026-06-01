"""Tests for hermes_cli.kanban_team_runtime — the Phase 3 runtime module.

Phase 3 (MASTER-PLAN §8) adds three runtime behaviors on top of the
Phase-1 team substrate, all in NEW modules so they don't collide with
Phase 2's edits to ``kanban_team.py`` / ``kanban_db.py``:

* Item 1 — per-member **hard shutdown** with a grace window
  (cooperative ``shutdown_response`` short-circuit, else SIGTERM-via-
  reclaim + archive so the dispatcher can't re-spawn).
* Item 2 — **auto-idle** on dispatcher run completion
  (terminal member-task run event auto-transitions the roster status).
* Item 3 — true **push delivery** (a new addressed comment re-readies
  the recipient's task so the dispatcher re-spawns it).

Everything is exercised with an **injected clock + injected kill fn +
injected event/wake fns** so no real subprocess is ever spawned and no
real signal is ever sent.

The ``conn`` fixture mirrors ``test_kanban_team.py``: a fresh isolated
``HERMES_HOME`` under ``tmp_path`` with the schema laid down.
"""
from __future__ import annotations

import signal
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_runtime as ktr

# The ``conn`` fixture is provided by conftest.py: it lays down core schema
# then the plugin's team_* tables via kts.ensure_tables (no longer core schema).


def _team_with_member(conn, member_name="worker"):
    """Create a team with one spawned member; return (team_id, member)."""
    team = kt.team_create(conn, name="alpha", goal="ship it")
    member = kt.team_spawn(
        conn, team.id, member_name, "teammate", "profile-a",
        "do the work",
    )
    return team.id, member


# ---------------------------------------------------------------------------
# Item 1 — per-member hard shutdown with grace window
# ---------------------------------------------------------------------------

def test_hard_shutdown_returns_early_when_response_arrives(conn):
    """Cooperative path: a ``shutdown_response`` from the member during the
    grace window short-circuits — the worker is NOT killed, the member is
    marked ``stopped``, and the outcome is ``cooperative``.
    """
    team_id, member = _team_with_member(conn)

    # The member replies to the lead with a shutdown_response BEFORE the
    # grace loop runs, so the very first inbox scan sees it.
    kt.team_send(
        conn, team_id,
        to="lead", from_sender="worker", message="ok shutting down",
        protocol_type="shutdown_response",
    )

    kill_mock = Mock()
    result = ktr.team_member_hard_shutdown(
        conn, team_id, "worker",
        timeout_seconds=30,
        signal_fn=kill_mock,
    )

    assert result["outcome"] == "cooperative"
    assert result["reclaimed"] is False
    # No kill ever attempted on the cooperative path.
    kill_mock.assert_not_called()
    # Roster reflects the deliberate stop.
    reloaded = kt.get_team(conn, team_id)
    assert reloaded.members["worker"].status == "stopped"


def test_hard_shutdown_escalates_after_timeout(conn, monkeypatch):
    """Timeout path: with no ``shutdown_response``, the running worker is
    terminated via reclaim (SIGTERM through the injected kill fn), the member
    task is **archived** (so the dispatcher can't re-spawn), and the member is
    marked ``stopped``.

    No real pid is targeted: ``_pid_alive`` is monkeypatched to ``False`` so
    the terminate grace loop exits on the first probe, and ``signal_fn`` is a
    ``Mock`` so no real signal is sent. The fake clock advances past the
    timeout on the first ``sleep_fn`` call.
    """
    team_id, member = _team_with_member(conn)
    task_id = member.task_id

    # Put the member task into the running state with a worker_pid, exactly
    # as the dispatcher would after spawning the one-shot worker. The default
    # claimer id is host-local ({host}:{pid}) so _terminate_reclaimed_worker
    # treats the lock as host-local and attempts termination.
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, 999999)

    # Never probe a live pid: pretend the worker is already gone so the
    # terminate grace loop returns immediately after the SIGTERM.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    kill_mock = Mock()
    # Fake clock: first now() is the loop start, the next call is already past
    # the timeout so the grace loop body runs zero times (no cooperative scan)
    # and we go straight to escalation. sleep_fn is never expected to be hit.
    clock = iter([0.0, 100.0, 100.0, 100.0])

    result = ktr.team_member_hard_shutdown(
        conn, team_id, "worker",
        timeout_seconds=30,
        now_fn=lambda: next(clock),
        sleep_fn=lambda _s: None,
        signal_fn=kill_mock,
    )

    assert result["outcome"] == "hard"
    assert result["reclaimed"] is True
    assert result["archived"] is True
    # SIGTERM was sent to the worker pid via the injected kill fn.
    kill_mock.assert_any_call(999999, signal.SIGTERM)
    # Member task ends archived — the dispatcher can never re-spawn it.
    assert kb.get_task(conn, task_id).status == "archived"
    # Roster reflects the deliberate stop.
    reloaded = kt.get_team(conn, team_id)
    assert reloaded.members["worker"].status == "stopped"


# ---------------------------------------------------------------------------
# Item 2 — auto-idle on dispatcher run completion
# ---------------------------------------------------------------------------

def _fake_event(eid, kind):
    """Minimal Event-shaped object for injected events_fn streams."""
    return kb.Event(id=eid, task_id="t", kind=kind, payload=None, created_at=eid)


def test_auto_idle_marks_member_idle_on_completed_run(conn):
    """A real `completed` member-task run auto-transitions the roster member
    to `idle` with no manual call."""
    team_id, member = _team_with_member(conn)
    # Member starts non-idle (spawning); drive a real run to completion.
    claimed = kb.claim_task(conn, member.task_id)
    assert claimed is not None
    assert kb.complete_task(conn, member.task_id, result="done") is True

    summary = ktr.run_auto_idle_pass(conn)

    reloaded = kt.get_team(conn, team_id)
    assert reloaded.members["worker"].status == "idle"
    # Pass reports what it transitioned.
    assert summary["transitions"] >= 1


def test_auto_idle_skips_terminal_members(conn):
    """A member already in a terminal status (`stopped`/`done`) is never
    reverted to idle, even if a terminal event exists for its task."""
    team_id, member = _team_with_member(conn)
    # Deliberately stop the member.
    kt.team_member_update(conn, team_id, "worker", status="stopped")

    # Inject a completed event for that member's task; it must be ignored.
    events = {member.task_id: [_fake_event(999, "completed")]}
    ktr.run_auto_idle_pass(conn, events_fn=lambda c, tid: events.get(tid, []))

    reloaded = kt.get_team(conn, team_id)
    assert reloaded.members["worker"].status == "stopped"


def test_auto_idle_archived_event_marks_member_stopped(conn):
    """An `archived` terminal event maps the roster member to `stopped`."""
    team_id, member = _team_with_member(conn)
    events = {member.task_id: [_fake_event(5, "archived")]}
    ktr.run_auto_idle_pass(conn, events_fn=lambda c, tid: events.get(tid, []))

    reloaded = kt.get_team(conn, team_id)
    assert reloaded.members["worker"].status == "stopped"


def test_auto_idle_is_idempotent(conn):
    """Re-running the pass after the cursor has advanced is a no-op: the
    second pass reports zero transitions and doesn't re-touch the member."""
    team_id, member = _team_with_member(conn)
    events = {member.task_id: [_fake_event(3, "completed")]}
    efn = lambda c, tid: events.get(tid, [])

    first = ktr.run_auto_idle_pass(conn, events_fn=efn)
    assert first["transitions"] >= 1
    assert kt.get_team(conn, team_id).members["worker"].status == "idle"

    # Manually flip back to running; a second pass must NOT re-apply the old
    # event (cursor already past id 3).
    kt.team_member_update(conn, team_id, "worker", status="running")
    second = ktr.run_auto_idle_pass(conn, events_fn=efn)
    assert second["transitions"] == 0
    assert kt.get_team(conn, team_id).members["worker"].status == "running"


def test_auto_idle_inert_with_no_teams(conn):
    """With no teams on the board the pass is a no-op."""
    summary = ktr.run_auto_idle_pass(conn)
    assert summary["teams"] == 0
    assert summary["transitions"] == 0


# ---------------------------------------------------------------------------
# Item 3 — true push-based message delivery
# ---------------------------------------------------------------------------

def test_new_addressed_comments_parses_recipients(conn):
    """`new_addressed_comments` returns (comment_id, recipient) for addressed
    mailbox comments, skipping [team:state] snapshots and unaddressed rows."""
    team_id, member = _team_with_member(conn)
    cid = kt.team_send(
        conn, team_id, to="worker", from_sender="lead", message="ping",
    )
    found = ktr.new_addressed_comments(conn, team_id, since_id=0)
    assert (int(cid), "worker") in [(c, r) for c, r in found]
    # The [team:state] comments are not addressed, so they don't appear.
    assert all(r for _c, r in found)


def test_push_wakes_idle_recipient(conn):
    """A new addressed comment to a non-running member wakes that member
    (re-readies its task) exactly once and advances push_cursor."""
    team_id, member = _team_with_member(conn)
    # Member is idle/non-running; send it a message.
    kt.team_member_update(conn, team_id, "worker", status="idle")
    kt.team_send(conn, team_id, to="worker", from_sender="lead", message="go")

    spy = Mock(return_value=True)
    summary = ktr.run_push_delivery_pass(conn, wake_fn=spy)

    spy.assert_called_once()
    # Called for the worker recipient.
    assert spy.call_args.args[2] == "worker" or "worker" in spy.call_args.args
    assert summary["woken"] == 1


def test_push_skips_lead_recipient(conn):
    """Messages addressed to @lead: are not member tasks and must not wake."""
    team_id, member = _team_with_member(conn)
    kt.team_send(conn, team_id, to="lead", from_sender="worker", message="hi")

    spy = Mock(return_value=True)
    summary = ktr.run_push_delivery_pass(conn, wake_fn=spy)
    spy.assert_not_called()
    assert summary["woken"] == 0


def test_push_idempotent(conn):
    """push_cursor is monotonic: a second pass over the same comments is a
    no-op."""
    team_id, member = _team_with_member(conn)
    kt.team_send(conn, team_id, to="worker", from_sender="lead", message="go")

    spy = Mock(return_value=True)
    first = ktr.run_push_delivery_pass(conn, wake_fn=spy)
    assert first["woken"] == 1
    second = ktr.run_push_delivery_pass(conn, wake_fn=spy)
    assert second["woken"] == 0
    assert spy.call_count == 1


def test_wake_member_reready_guarded(conn):
    """`wake_member` re-readies a non-running member task but refuses to
    resurrect a running/archived/done task."""
    team_id, member = _team_with_member(conn)
    task_id = member.task_id

    # Drive to running, then archive (terminal). wake must refuse.
    kb.claim_task(conn, task_id)
    assert ktr.wake_member(conn, team_id, "worker") is False  # running → guarded
    kb.archive_task(conn, task_id)
    assert ktr.wake_member(conn, team_id, "worker") is False  # archived → guarded


def test_wake_member_reready_idle_task(conn):
    """A member task sitting in a non-terminal, non-running status is
    re-readied by wake_member."""
    team_id, member = _team_with_member(conn)
    task_id = member.task_id
    # Member task is created `ready`; force it to `todo` so wake has work to do.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task_id,))
    assert ktr.wake_member(conn, team_id, "worker") is True
    assert kb.get_task(conn, task_id).status == "ready"


def test_push_inert_with_no_teams(conn):
    """With no teams the push pass is a no-op."""
    summary = ktr.run_push_delivery_pass(conn)
    assert summary["teams"] == 0
    assert summary["woken"] == 0
