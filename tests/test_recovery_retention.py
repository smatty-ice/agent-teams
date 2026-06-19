"""Tests for the requeue cursor-rewind fix and history retention (prune)."""
from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_store as kts
from plugins.agent_teams import kanban_team_recovery as ktr


def _first_msg_row_id(conn, team_id, to_member):
    row = conn.execute(
        "SELECT id FROM team_messages WHERE team_id = ? AND to_member = ? "
        "ORDER BY id ASC LIMIT 1",
        (team_id, to_member),
    ).fetchone()
    return int(row["id"])


def test_requeue_resurfaces_message_after_ack(conn):
    """After a member acks past a message, requeue makes it unread again —
    reconciling the team_messages index with the comment-cursor read model."""
    rec = kt.team_create(conn, name="reqteam", goal="g")
    kt.team_spawn(conn, rec.id, "alice", "r", "p_alice", "do x")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="hello")

    inbox = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    assert any(m.body == "hello" for m in inbox)
    last_id = max(int(m.id) for m in inbox)
    kt.team_inbox_ack(conn, rec.id, member="alice", through_id=str(last_id))
    assert kt.team_inbox(conn, rec.id, member="alice", unread_only=True) == []

    ktr.requeue(conn, _first_msg_row_id(conn, rec.id, "alice"))
    again = kt.team_inbox(conn, rec.id, member="alice", unread_only=True)
    assert any(m.body == "hello" for m in again)


def test_prune_history_deletes_aged_terminal_rows_only(conn):
    rec = kt.team_create(conn, name="pruneteam", goal="g")
    kt.team_spawn(conn, rec.id, "bob", "r", "p_bob", "do y")
    kt.team_send(conn, rec.id, to="bob", from_sender="lead", message="m1")
    inbox = kt.team_inbox(conn, rec.id, member="bob", unread_only=True)
    kt.team_inbox_ack(
        conn, rec.id, member="bob",
        through_id=str(max(int(m.id) for m in inbox)),
    )

    old = int(time.time()) - 40 * 86400  # past the 30d cutoff
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE team_messages SET acked_at = ? WHERE team_id = ?",
            (old, rec.id),
        )
        conn.execute(
            "INSERT INTO team_operations (operation_id, team_id, verb, "
            "input_hash, status, created_at, updated_at) "
            "VALUES ('op_old', ?, 'team_send', 'h', 'done', ?, ?)",
            (rec.id, old, old),
        )
        conn.execute(
            "INSERT INTO team_operations (operation_id, team_id, verb, "
            "input_hash, status, created_at, updated_at) "
            "VALUES ('op_pending', ?, 'team_send', 'h2', 'pending', ?, ?)",
            (rec.id, old, old),
        )

    res = kts.prune_history(conn, older_than_seconds=30 * 86400)
    assert res["messages_deleted"] >= 1
    assert res["operations_deleted"] >= 1

    def _count(op_id):
        return conn.execute(
            "SELECT COUNT(*) AS c FROM team_operations WHERE operation_id = ?",
            (op_id,),
        ).fetchone()["c"]

    assert _count("op_pending") == 1   # pending never pruned, even when old
    assert _count("op_old") == 0       # terminal + aged -> pruned


def test_prune_history_keeps_recent(conn):
    rec = kt.team_create(conn, name="recentteam", goal="g")
    kt.team_spawn(conn, rec.id, "cara", "r", "p_cara", "do z")
    kt.team_send(conn, rec.id, to="cara", from_sender="lead", message="fresh")
    inbox = kt.team_inbox(conn, rec.id, member="cara", unread_only=True)
    kt.team_inbox_ack(
        conn, rec.id, member="cara",
        through_id=str(max(int(m.id) for m in inbox)),
    )
    res = kts.prune_history(conn, older_than_seconds=30 * 86400)
    assert res["messages_deleted"] == 0  # recently acked -> kept
