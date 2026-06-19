"""Tests for the team_state table refactor.

Roster / inbox-cursors / push-cursor moved out of append-only ``[team:state]``
comments (read back by created_at at 1s granularity, with a non-atomic
read-modify-write) into one authoritative ``team_state`` SQLite row. These tests
pin the new guarantees: table round-trip, atomic merge persisted to the table,
no unbounded state-comment growth, and back-compat read of legacy comment state.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_store as kts

STATE_PREFIX = kt.STATE_PREFIX


def _state_comments(conn, root_id):
    return [
        c for c in kb.list_comments(conn, root_id)
        if (c.body or "").startswith(STATE_PREFIX)
    ]


def test_state_write_read_roundtrip(conn):
    with kb.write_txn(conn):
        kts.state_write(conn, "t_x", {"roster": {"a": {"name": "a"}}, "n": 1})
    assert kts.state_read(conn, "t_x") == {"roster": {"a": {"name": "a"}}, "n": 1}


def test_state_read_missing_returns_none(conn):
    # None (not {}) so callers can distinguish "no row" from "empty state".
    assert kts.state_read(conn, "t_nope") is None


def test_update_state_persists_to_table(conn):
    rec = kt.team_create(conn, name="statey", goal="g")
    kt._update_state(conn, rec.id, cursors={"alice": 3})
    row = kts.state_read(conn, rec.id)
    assert row is not None and row.get("cursors") == {"alice": 3}
    assert kt._read_state(conn, rec.id).get("cursors") == {"alice": 3}


def test_no_state_comment_growth(conn):
    """Many updates -> no per-update [team:state] comment accrual, and exactly
    one authoritative table row reflecting the last write."""
    rec = kt.team_create(conn, name="growthy", goal="g")
    before = len(_state_comments(conn, rec.id))
    for i in range(10):
        kt._update_state(conn, rec.id, cursors={"m": i})
    assert len(_state_comments(conn, rec.id)) == before  # no new state comments
    assert kts.state_read(conn, rec.id).get("cursors") == {"m": 9}
    n_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM team_state WHERE team_id = ?", (rec.id,)
    ).fetchone()["n"]
    assert n_rows == 1


def test_backcompat_reads_legacy_comment_then_migrates(conn):
    """A pre-table team (state only in a legacy comment, no table row) stays
    readable, and the next _update_state migrates + merges it into the table."""
    rec = kt.team_create(conn, name="legacy", goal="g")
    with kb.write_txn(conn):
        conn.execute("DELETE FROM team_state WHERE team_id = ?", (rec.id,))
    legacy = {"roster": {"old": {"name": "old"}}, "cursors": {"old": 7}}
    kb.add_comment(
        conn, rec.id, author="team-lead",
        body=STATE_PREFIX + json.dumps(legacy, sort_keys=True),
    )
    assert kts.state_read(conn, rec.id) is None          # table empty
    assert kt._read_state(conn, rec.id) == legacy        # falls back to comment

    kt._update_state(conn, rec.id, cursors={"old": 8})
    row = kts.state_read(conn, rec.id)
    assert row is not None
    assert row["roster"] == {"old": {"name": "old"}}     # preserved from legacy
    assert row["cursors"] == {"old": 8}                  # merged


def test_backcompat_newest_comment_by_id_wins(conn):
    """Legacy fallback picks the newest comment by id — deterministic even if
    created_at ties at 1s granularity (the old same-second bug)."""
    rec = kt.team_create(conn, name="tiebreak", goal="g")
    with kb.write_txn(conn):
        conn.execute("DELETE FROM team_state WHERE team_id = ?", (rec.id,))
    kb.add_comment(conn, rec.id, author="team-lead",
                   body=STATE_PREFIX + json.dumps({"v": 1}))
    kb.add_comment(conn, rec.id, author="team-lead",
                   body=STATE_PREFIX + json.dumps({"v": 2}))
    assert kt._read_state(conn, rec.id) == {"v": 2}
