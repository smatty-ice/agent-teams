"""Tests for hermes_cli.kanban_team — the agent-teams domain module.

Mirrors the test layout of ``test_kanban_swarm.py`` (the closest design
precedent in this repo). Each fixture-using test opens a fresh isolated
``HERMES_HOME`` under ``tmp_path``, lets ``kanban_db.init_db()`` lay down
the schema there, and operates on the resulting connection.

These tests pin the **R2** substrate decisions from
``docs/plans-eval/MASTER-PLAN.md`` §2:

* member + work tasks are stand-alone (no team-root parent link) — §2.2
* team grouping is via ``tenant=team:<name>`` (indexed) — §2.6
* per-task ownership via the ``[owner:@<member>]`` first-line body header — §2.3
* mailbox sender is the comment author, no ``[from:X]`` body prefix — ISS-6
* slug regex defangs SQL-LIKE wildcards — §2.7 / F6
* ``team_delete`` archives the root **and** every member task — ISS-5
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt

# The ``conn`` fixture is provided by conftest.py: it lays down core schema
# (kb.init_db) then the plugin's team_* tables (kts.ensure_tables), which are
# no longer part of core schema.


# ---------------------------------------------------------------------------
# Slug validation (MASTER-PLAN §2.7 / F6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "  ", "a/b", "a\\b", "a:b", "a]b", "a[b",
                                 "a%b", "a_b", ".", "..", "-lead", "@x"])
def test_require_slug_rejects_unsafe_names(bad):
    with pytest.raises(ValueError):
        kt._require_slug(bad, "name")


@pytest.mark.parametrize("good", ["alpha", "a", "team-1", "Researcher2"])
def test_require_slug_accepts_safe_names(good):
    assert kt._require_slug(good, "name") == good


# ---------------------------------------------------------------------------
# Task 1 — state round-trip (MASTER-PLAN.md §6 Step 1.1)
# ---------------------------------------------------------------------------

def test_state_comment_round_trip(conn):
    """`_write_state` then `_read_state` recovers the same dict."""
    root_id = kb.create_task(
        conn,
        title="[team:test]",
        assignee="team-lead",
        created_by="team-lead",
        initial_status="blocked",
    )
    kt._write_state(conn, root_id, {"roster": {}, "cursors": {}, "ops": {}})
    assert kt._read_state(conn, root_id) == {"roster": {}, "cursors": {}, "ops": {}}


# ---------------------------------------------------------------------------
# Task 2 — team_create / get_team / list_teams
# ---------------------------------------------------------------------------

def test_team_create_returns_team_record(conn):
    rec = kt.team_create(conn, name="alpha", goal="test goal")
    assert rec.name == "alpha"
    assert rec.goal == "test goal"
    assert rec.members == {}
    assert rec.id


def test_team_create_root_task_is_blocked_and_tenanted(conn):
    rec = kt.team_create(conn, name="beta", goal="goal")
    task = kb.get_task(conn, rec.id)
    assert task is not None
    assert task.status == "blocked"
    assert task.title == "[team:beta]"
    # R2 §2.6: root carries the team tenant for indexed lookup.
    assert task.tenant == "team:beta"


def test_get_team_round_trip(conn):
    rec = kt.team_create(conn, name="gamma", goal="goal-g")
    loaded = kt.get_team(conn, rec.id)
    assert loaded.id == rec.id
    assert loaded.name == "gamma"
    assert loaded.goal == "goal-g"


def test_list_teams_returns_all_non_archived(conn):
    a = kt.team_create(conn, name="alpha2", goal="g1")
    b = kt.team_create(conn, name="beta2", goal="g2")
    teams = kt.list_teams(conn)
    names = {t.name for t in teams}
    assert {"alpha2", "beta2"}.issubset(names)
    assert a.id in {t.id for t in teams}
    assert b.id in {t.id for t in teams}


def test_list_teams_excludes_member_cards(conn):
    """Member registry cards share the ``[team:`` title prefix but must not
    be enumerated as teams."""
    rec = kt.team_create(conn, name="solo", goal="g")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    teams = kt.list_teams(conn)
    assert [t.name for t in teams] == ["solo"]


# ---------------------------------------------------------------------------
# Task 3 — team_spawn (stand-alone task, tenant, owner header)
# ---------------------------------------------------------------------------

def test_team_spawn_adds_member_to_roster(conn):
    rec = kt.team_create(conn, name="delta", goal="goal")
    member = kt.team_spawn(
        conn, rec.id,
        "researcher", "researcher", "researcher-profile", "do research",
    )
    assert member.name == "researcher"
    assert member.profile == "researcher-profile"
    assert member.role == "researcher"
    assert member.status == "spawning"
    assert member.task_id

    member_task = kb.get_task(conn, member.task_id)
    assert member_task is not None
    assert member_task.assignee == "researcher-profile"
    # R2 §2.3: ownership header is the first line of the body.
    assert member_task.body.startswith("[owner:@researcher]")
    # R2 §2.6: tenant groups team tasks.
    assert member_task.tenant == "team:delta"
    # R2 §2.2: stand-alone — no parent link to the team root, and the kernel
    # parks a no-parent task in 'ready' (claimable) without a raw UPDATE.
    assert member_task.status == "ready"

    loaded = kt.get_team(conn, rec.id)
    assert "researcher" in loaded.members
    assert loaded.members["researcher"].profile == "researcher-profile"


def test_team_spawn_member_task_has_no_parent_link(conn):
    """R2 §2.2 (BUG-4): member task must NOT be a child of the team root."""
    rec = kt.team_create(conn, name="delta2", goal="goal")
    member = kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_links WHERE child_id = ?",
        (member.task_id,),
    ).fetchone()
    assert row["n"] == 0


def test_team_spawn_rejects_duplicate_name(conn):
    rec = kt.team_create(conn, name="epsilon", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    with pytest.raises(ValueError, match="already has a member"):
        kt.team_spawn(conn, rec.id, "researcher", "r2", "p2", "q2")


# ---------------------------------------------------------------------------
# Task 4 — team_task_create
# ---------------------------------------------------------------------------

def test_team_task_create_writes_owner_header_and_tenant(conn):
    rec = kt.team_create(conn, name="zeta", goal="goal")
    task_id = kt.team_task_create(conn, rec.id, "Write the report")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.title == "Write the report"
    # Unassigned → owner sentinel '-'
    assert task.body.startswith("[owner:@-]")
    assert task.tenant == "team:zeta"


def test_team_task_create_resolves_owner_to_profile(conn):
    rec = kt.team_create(conn, name="eta", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "researcher", "rp", "do research")
    task_id = kt.team_task_create(conn, rec.id, "Write report", owner="researcher")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.assignee == "rp"
    assert task.body.startswith("[owner:@researcher]")


def test_team_task_create_propagates_dependencies(conn):
    rec = kt.team_create(conn, name="eta2", goal="goal")
    dep_id = kt.team_task_create(conn, rec.id, "Research phase")
    task_id = kt.team_task_create(
        conn, rec.id, "Write report", dependencies=[dep_id],
    )
    # Kanban honors task_links — child parks in 'todo' until parent is done.
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "todo"
    link = conn.execute(
        "SELECT COUNT(*) AS n FROM task_links WHERE parent_id = ? AND child_id = ?",
        (dep_id, task_id),
    ).fetchone()
    assert link["n"] == 1


def test_team_task_create_rejects_unknown_owner(conn):
    rec = kt.team_create(conn, name="eta3", goal="goal")
    with pytest.raises(ValueError, match="no member named"):
        kt.team_task_create(conn, rec.id, "x", owner="ghost")


# ---------------------------------------------------------------------------
# Task 5 — team_send / team_inbox / team_inbox_ack
# ---------------------------------------------------------------------------

def test_team_send_writes_addressed_comment_with_author_as_sender(conn):
    rec = kt.team_create(conn, name="theta", goal="goal")
    msg_id = kt.team_send(
        conn, rec.id, to="researcher", from_sender="lead",
        message="please check section 3",
    )
    assert msg_id
    comments = kb.list_comments(conn, rec.id)
    matching = [c for c in comments if c.body.startswith("@researcher:")]
    assert len(matching) == 1
    assert "please check section 3" in matching[0].body
    # R2 ISS-6: sender authority is the comment author, not a body prefix.
    assert matching[0].author == "lead"
    assert "[from:" not in matching[0].body


def test_team_send_protocol_type_prefixed(conn):
    rec = kt.team_create(conn, name="iota", goal="goal")
    kt.team_send(
        conn, rec.id, to="researcher", from_sender="lead",
        message="please shut down",
        protocol_type="shutdown_request",
    )
    comments = kb.list_comments(conn, rec.id)
    matching = [c for c in comments if "[shutdown_request]" in c.body]
    assert len(matching) == 1


def test_team_send_rejects_unknown_protocol_type(conn):
    rec = kt.team_create(conn, name="iota2", goal="goal")
    with pytest.raises(ValueError, match="protocol_type"):
        kt.team_send(conn, rec.id, to="researcher", from_sender="lead",
                     message="x", protocol_type="bogus")


def test_team_inbox_returns_addressed_messages(conn):
    rec = kt.team_create(conn, name="kappa", goal="goal")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="hello alice")
    kt.team_send(conn, rec.id, to="bob",   from_sender="lead", message="hello bob")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="second msg")
    alice_msgs = kt.team_inbox(conn, rec.id, member="alice", unread_only=False)
    assert len(alice_msgs) == 2
    assert all(m.to == "alice" and m.from_sender == "lead" for m in alice_msgs)
    bob_msgs = kt.team_inbox(conn, rec.id, member="bob", unread_only=False)
    assert len(bob_msgs) == 1


def test_team_inbox_unread_cursor_strict_monotonic(conn):
    rec = kt.team_create(conn, name="lam", goal="goal")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="msg1")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="msg2")
    msgs = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    assert len(msgs) == 2
    kt.team_inbox_ack(conn, rec.id, member="alice", through_id=msgs[-1].id)
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []
    # A stale (lower) ack must not move the cursor backwards.
    kt.team_inbox_ack(conn, rec.id, member="alice",
                      through_id=str(int(msgs[0].id) - 1))
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []


# ---------------------------------------------------------------------------
# Task 6 — team_member_update / team_status / team_shutdown / team_delete
# ---------------------------------------------------------------------------

def test_team_member_update_changes_status(conn):
    rec = kt.team_create(conn, name="mu", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    kt.team_member_update(conn, rec.id, "researcher", status="idle")
    loaded = kt.get_team(conn, rec.id)
    assert loaded.members["researcher"].status == "idle"


def test_team_status_counts_only_work_tasks(conn):
    rec = kt.team_create(conn, name="nu", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    kt.team_task_create(conn, rec.id, "Write report", owner="researcher")
    kt.team_task_create(conn, rec.id, "Read papers")  # unassigned
    status = kt.team_status(conn, rec.id)
    assert status["name"] == "nu"
    assert "researcher" in status["members"]
    # 2 work tasks; the root + member registry cards are excluded.
    assert status["tasks"]["total"] == 2


def test_team_shutdown_sends_protocol_message(conn):
    rec = kt.team_create(conn, name="xi", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    kt.team_shutdown(conn, rec.id, member="researcher")
    msgs = kt.team_inbox(conn, rec.id, member="researcher", unread_only=False)
    assert any(m.protocol_type == "shutdown_request" for m in msgs)


def test_team_delete_refuses_active_members(conn):
    rec = kt.team_create(conn, name="omicron", goal="goal")
    kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    with pytest.raises(RuntimeError, match="active members"):
        kt.team_delete(conn, rec.id)


def test_team_delete_archives_root_and_member_tasks(conn):
    """R2 ISS-5: deleting a team must archive each member task too, so the
    dispatcher can never re-claim an orphaned member task."""
    rec = kt.team_create(conn, name="pi", goal="goal")
    member = kt.team_spawn(conn, rec.id, "researcher", "r", "p", "q")
    kt.team_member_update(conn, rec.id, "researcher", status="stopped")
    kt.team_delete(conn, rec.id)
    root_task = kb.get_task(conn, rec.id)
    member_task = kb.get_task(conn, member.task_id)
    assert root_task is not None and root_task.status == "archived"
    assert member_task is not None and member_task.status == "archived"


# ---------------------------------------------------------------------------
# Task 9 — full-lifecycle smoke + idempotency stress
# ---------------------------------------------------------------------------

def test_full_team_lifecycle(conn):
    """End-to-end happy path: create → spawn(×2) → task → DM ↔ inbox/ack →
    shutdown → delete. Mirrors the plan-b acceptance criteria.
    """
    rec = kt.team_create(conn, name="lifecycle", goal="ship it")
    alice = kt.team_spawn(conn, rec.id, "alice", "researcher", "p-alice", "do research")
    bob   = kt.team_spawn(conn, rec.id, "bob",   "writer",     "p-bob",   "write up")

    assert alice.task_id and bob.task_id
    loaded = kt.get_team(conn, rec.id)
    assert {"alice", "bob"}.issubset(loaded.members)

    # Lead DMs each teammate
    kt.team_send(conn, rec.id, to="alice", from_sender="lead",
                 message="please start now")
    kt.team_send(conn, rec.id, to="bob", from_sender="lead",
                 message="wait for alice's findings")

    # Each teammate reads their inbox; cursors are independent
    alice_msgs = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    bob_msgs   = kt.team_inbox(conn, rec.id, member="bob",   unread_only=True)
    assert len(alice_msgs) == 1
    assert len(bob_msgs) == 1
    assert alice_msgs[0].body == "please start now"
    assert alice_msgs[0].from_sender == "lead"

    # alice acks then re-reads → empty
    kt.team_inbox_ack(conn, rec.id, member="alice", through_id=alice_msgs[-1].id)
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []

    # Lead creates dependent work; team_status reflects the new tasks
    research_id = kt.team_task_create(conn, rec.id, "Research phase",
                                      owner="alice")
    writeup_id  = kt.team_task_create(conn, rec.id, "Write report",
                                      owner="bob", dependencies=[research_id])
    status = kt.team_status(conn, rec.id)
    assert status["name"] == "lifecycle"
    assert status["tasks"]["total"] == 2
    assert research_id != writeup_id

    # Cooperative shutdown — sends protocol message; idle until ack received
    kt.team_shutdown(conn, rec.id, member="alice")
    kt.team_shutdown(conn, rec.id, member="bob")
    alice_inbox = kt.team_inbox(conn, rec.id, member="alice", unread_only=False)
    assert any(m.protocol_type == "shutdown_request" for m in alice_inbox)

    # Lead marks members stopped (would come from shutdown_response in real flow)
    kt.team_member_update(conn, rec.id, "alice", status="stopped")
    kt.team_member_update(conn, rec.id, "bob",   status="stopped")

    # Now delete succeeds — root AND member tasks archive cleanly
    kt.team_delete(conn, rec.id)
    assert kb.get_task(conn, rec.id).status == "archived"
    assert kb.get_task(conn, alice.task_id).status == "archived"
    assert kb.get_task(conn, bob.task_id).status == "archived"


def test_team_send_is_idempotent_by_input_hash(tmp_path, monkeypatch):
    """Plan-b acceptance: duplicate team_send tool calls with identical
    args return the cached result, write the comment only once.
    """
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    try:
        kb._INITIALIZED_PATHS.clear()
    except AttributeError:
        pass
    kb.init_db()
    conn = kb.connect()
    try:
        from plugins.agent_teams import team_tools as tt
        from plugins.agent_teams import kanban_team_store as kts
        kts.ensure_tables(conn)  # team_* tables aren't in core schema anymore
        rec = kt.team_create(conn, name="dedup", goal="goal")
        args = {"team_id": rec.id, "to": "alice", "message": "hi",
                "sender": "lead"}
        r1 = tt._handle_team_send(args)
        r2 = tt._handle_team_send(args)
        # Same response JSON; comment written exactly once.
        assert r1 == r2
        addressed = [c for c in kb.list_comments(conn, rec.id)
                     if c.body.startswith("@alice:")]
        assert len(addressed) == 1, (
            f"expected 1 addressed comment, got {len(addressed)}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 2 — Item 1: team_messages table (delivered/acked/dead-letter)
# ---------------------------------------------------------------------------

def _msg_rows(conn, team_id):
    return conn.execute(
        "SELECT * FROM team_messages WHERE team_id = ? ORDER BY id ASC",
        (team_id,),
    ).fetchall()


def test_team_send_still_writes_addressed_comment(conn):
    """CONTRACT (locks Phase 3's notifier signal): team_send must keep
    writing the addressed task_comment AND firing the `commented` event.
    The team_messages row is an *additional* index, never a replacement.
    """
    rec = kt.team_create(conn, name="contract", goal="goal")
    events_before = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND kind = 'commented'",
        (rec.id,),
    ).fetchone()["n"]

    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="ping")

    # The addressed comment is still on the root.
    addressed = [
        c for c in kb.list_comments(conn, rec.id)
        if c.body.startswith("@alice:")
    ]
    assert len(addressed) == 1
    # And a new `commented` event fired (the Phase 3 wake-up signal).
    events_after = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND kind = 'commented'",
        (rec.id,),
    ).fetchone()["n"]
    assert events_after == events_before + 1


def test_team_messages_row_written_on_send(conn):
    rec = kt.team_create(conn, name="msgrow", goal="goal")
    msg_id = kt.team_send(
        conn, rec.id, to="alice", from_sender="lead",
        message="hello", protocol_type="plan_approval_request",
    )
    rows = _msg_rows(conn, rec.id)
    assert len(rows) == 1
    r = rows[0]
    assert r["to_member"] == "alice"
    assert r["from_sender"] == "lead"
    assert r["protocol_type"] == "plan_approval_request"
    assert r["body"] == "hello"
    assert int(r["comment_id"]) == int(msg_id)
    assert r["delivered_at"] is None
    assert r["acked_at"] is None
    assert int(r["dead_letter"]) == 0


def test_team_inbox_stamps_delivered_at(conn):
    rec = kt.team_create(conn, name="deliver", goal="goal")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="m1")
    # Before reading: not delivered.
    assert _msg_rows(conn, rec.id)[0]["delivered_at"] is None
    kt.team_inbox(conn, rec.id, member="alice", unread_only=False)
    # After reading: delivered_at stamped.
    row = _msg_rows(conn, rec.id)[0]
    assert row["delivered_at"] is not None
    first_stamp = row["delivered_at"]
    # Re-reading does not move the timestamp (stamp once).
    kt.team_inbox(conn, rec.id, member="alice", unread_only=False)
    assert _msg_rows(conn, rec.id)[0]["delivered_at"] == first_stamp


def test_team_inbox_ack_stamps_acked_at(conn):
    rec = kt.team_create(conn, name="ackstamp", goal="goal")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="m1")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="m2")
    msgs = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    assert len(msgs) == 2
    kt.team_inbox_ack(conn, rec.id, member="alice", through_id=msgs[-1].id)
    rows = _msg_rows(conn, rec.id)
    assert all(r["acked_at"] is not None for r in rows)


def test_team_send_to_stopped_member_dead_letters(conn):
    rec = kt.team_create(conn, name="deadletter", goal="goal")
    kt.team_spawn(conn, rec.id, "alice", "r", "p-alice", "do work")
    kt.team_member_update(conn, rec.id, "alice", status="stopped")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="too late")
    rows = _msg_rows(conn, rec.id)
    assert len(rows) == 1
    assert int(rows[0]["dead_letter"]) == 1


# ---------------------------------------------------------------------------
# Phase 2 — Item 8: board-qualified inbox cursor keys
# ---------------------------------------------------------------------------

def test_cursor_key_is_board_qualified(conn):
    """After an ack, the cursor is stored under a board-qualified key
    (``<board>:<member>``), not a bare ``<member>`` key."""
    rec = kt.team_create(conn, name="boardq", goal="goal", board="proj-x")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="m1")
    msgs = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    kt.team_inbox_ack(conn, rec.id, member="alice", through_id=msgs[-1].id)

    state = kt._read_state(conn, rec.id)
    cursors = state.get("cursors") or {}
    qualified = kt._cursor_key("proj-x", "alice")
    assert qualified in cursors
    assert "alice" not in cursors  # not the bare key
    # And unread filtering still works through the qualified key.
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []


def test_legacy_bare_cursor_still_honored(conn):
    """A cursor written by Phase 1 code under the bare ``<member>`` key must
    still suppress already-acked messages after the upgrade."""
    rec = kt.team_create(conn, name="legacy", goal="goal", board="proj-y")
    cid = kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="old")
    # Simulate a Phase-1 cursor: bare key, no board qualifier.
    state = kt._read_state(conn, rec.id)
    state["cursors"] = {"alice": int(cid)}
    kt._write_state(conn, rec.id, state)

    # The legacy bare cursor is honored: the old message is filtered out.
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []


# ---------------------------------------------------------------------------
# Phase 2 — Item 5: plan-approval enforcement (scoped to team_task_create)
# ---------------------------------------------------------------------------

def test_member_flagged_requires_approval_blocks_task_create(conn):
    """A member flagged requires_plan_approval cannot have a task created for
    them until a plan_approval_response involving them is recorded. No task
    row is created on refusal."""
    rec = kt.team_create(conn, name="gate", goal="goal")
    kt.team_spawn(conn, rec.id, "impl", "implementer", "p-impl", "do work")
    kt.team_member_update(conn, rec.id, "impl", requires_plan_approval=True)

    before = len(kb.list_tasks(conn, tenant=kt._team_tenant("gate")))
    with pytest.raises(kt.PlanApprovalRequired):
        kt.team_task_create(conn, rec.id, "Implement X", owner="impl")
    after = len(kb.list_tasks(conn, tenant=kt._team_tenant("gate")))
    assert after == before  # nothing created


def test_plan_approval_response_unblocks(conn):
    rec = kt.team_create(conn, name="gate2", goal="goal")
    kt.team_spawn(conn, rec.id, "impl", "implementer", "p-impl", "do work")
    kt.team_member_update(conn, rec.id, "impl", requires_plan_approval=True)
    # The member sends a plan_approval_response (approving the plan).
    kt.team_send(
        conn, rec.id, to="lead", from_sender="impl",
        message="approved", protocol_type="plan_approval_response",
    )
    # Now task creation is allowed.
    task_id = kt.team_task_create(conn, rec.id, "Implement X", owner="impl")
    assert task_id


def test_unflagged_member_not_blocked(conn):
    rec = kt.team_create(conn, name="gate3", goal="goal")
    kt.team_spawn(conn, rec.id, "impl", "implementer", "p-impl", "do work")
    # No requires_plan_approval flag → never gated.
    task_id = kt.team_task_create(conn, rec.id, "Implement X", owner="impl")
    assert task_id


def test_refusal_recorded_in_journal(tmp_path, monkeypatch):
    """The tool-layer handler journals a status='refused' op when the gate
    blocks a team_task_create."""
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    try:
        kb._INITIALIZED_PATHS.clear()
    except AttributeError:
        pass
    kb.init_db()
    conn = kb.connect()
    try:
        from plugins.agent_teams import team_tools as tt
        from plugins.agent_teams import kanban_team_store as kts
        kts.ensure_tables(conn)  # team_* tables aren't in core schema anymore
        rec = kt.team_create(conn, name="gatej", goal="goal")
        kt.team_spawn(conn, rec.id, "impl", "implementer", "p-impl", "do work")
        kt.team_member_update(conn, rec.id, "impl", requires_plan_approval=True)

        out = tt._handle_team_task_create(
            {"team_id": rec.id, "title": "Implement X", "owner": "impl"}
        )
        assert "error" in out.lower() or "plan approval" in out.lower()
        # A refused op is journaled.
        refused = kts.list_operations(conn, team_id=rec.id, status="refused")
        assert len(refused) >= 1
        assert refused[0]["verb"] == "team_task_create"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 2 — Item 6: branch/workspace collision safety hook
# ---------------------------------------------------------------------------

def test_team_spawn_rejects_duplicate_branch(conn):
    """Spawning a worktree teammate on a branch_name already held by a live
    task is refused, and the second member is not added to the roster."""
    rec = kt.team_create(conn, name="branchteam", goal="goal")
    kt.team_spawn(
        conn, rec.id, "impl-a", "implementer", "p-a", "work a",
        branch_name="wt/feature", workspace_kind="worktree",
    )
    before = len(kt.get_team(conn, rec.id).members)
    with pytest.raises(ValueError, match="branch"):
        kt.team_spawn(
            conn, rec.id, "impl-b", "implementer", "p-b", "work b",
            branch_name="wt/feature", workspace_kind="worktree",
        )
    after = len(kt.get_team(conn, rec.id).members)
    assert after == before  # impl-b not added


def test_branch_check_ignores_archived(conn):
    """An archived task holding the branch_name does not block a new spawn on
    the same branch."""
    rec = kt.team_create(conn, name="brancharch", goal="goal")
    m = kt.team_spawn(
        conn, rec.id, "impl-a", "implementer", "p-a", "work a",
        branch_name="wt/recycle", workspace_kind="worktree",
    )
    kb.archive_task(conn, m.task_id)
    # Same branch is now free because the holder is archived.
    m2 = kt.team_spawn(
        conn, rec.id, "impl-b", "implementer", "p-b", "work b",
        branch_name="wt/recycle", workspace_kind="worktree",
    )
    assert m2.task_id


# ---------------------------------------------------------------------------
# Phase 2 — Item 10: subagent-definition reuse (agent_type, thin slice)
# ---------------------------------------------------------------------------

def _write_skill(home_dir, name, body):
    """Create a SKILL.md under <home>/skills/agents/<name>/ and return it."""
    skill_dir = home_dir / "skills" / "agents" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: a test agent\n---\n\n{body}\n"
    )
    return md


def test_team_spawn_agent_type_appends_skill_and_body(conn, tmp_path):
    home = tmp_path / ".hermes"
    _write_skill(home, "deep-researcher", "You are a meticulous researcher.")

    rec = kt.team_create(conn, name="atype", goal="goal")
    member = kt.team_spawn(
        conn, rec.id, "alice", "researcher", "p-alice", "investigate X",
        agent_type="deep-researcher",
    )
    task = kb.get_task(conn, member.task_id)
    # The skill name is appended to the task's skills list.
    assert task.skills is not None and "deep-researcher" in task.skills
    # The skill body (frontmatter stripped) is appended to the prompt body.
    assert "You are a meticulous researcher." in (task.body or "")
    # The frontmatter itself is NOT carried into the prompt.
    assert "description: a test agent" not in (task.body or "")


def test_team_spawn_unknown_agent_type_errors(conn, tmp_path):
    rec = kt.team_create(conn, name="atype2", goal="goal")
    with pytest.raises(ValueError, match="agent_type"):
        kt.team_spawn(
            conn, rec.id, "alice", "researcher", "p-alice", "investigate X",
            agent_type="nonexistent-agent",
        )
