"""Tests for hermes_cli.kanban_team_store — the durable message + operation
journal layer (Phase 2 Items 1, 2).

Each fixture-using test opens a fresh isolated ``HERMES_HOME`` under
``tmp_path`` and lets ``kanban_db.init_db()`` lay down the schema, mirroring
``test_kanban_team.py``.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_store as store

# The ``conn`` fixture (conftest.py) lays down core schema then the plugin's
# team_* tables via ``store.ensure_tables`` — these tables are no longer part
# of core schema.


# ---------------------------------------------------------------------------
# Item 2 — team_operations journal table
# ---------------------------------------------------------------------------

def test_team_operations_table_exists_after_init(conn):
    """``team_operations`` and its dedup indexes are created by the plugin's
    ``ensure_tables`` (run by the conn fixture) — no longer core SCHEMA_SQL."""
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "team_operations" in tables

    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_team_ops_hash" in indexes
    assert "idx_team_ops_team" in indexes


def test_op_dedup_by_verb_and_hash(conn):
    """A second op_begin/op_finish for the same (verb, args) returns the
    journaled prior result instead of running again — and survives a fresh
    connection (durable, not in-memory)."""
    args = {"team_id": "t_1", "to": "alice", "message": "hi"}

    # No prior op → lookup is a miss.
    assert store.op_lookup(conn, operation_id=None, verb="team_send", args=args) is None

    op_id = store.op_begin(conn, operation_id=None, team_id="t_1", verb="team_send", args=args)
    store.op_finish(conn, op_id, status="done", result_json='{"message_id": "5"}')

    # Same (verb, args) → cached result returned.
    hit = store.op_lookup(conn, operation_id=None, verb="team_send", args=args)
    assert hit == '{"message_id": "5"}'

    # Different args → distinct op, miss.
    other = {"team_id": "t_1", "to": "bob", "message": "hi"}
    assert store.op_lookup(conn, operation_id=None, verb="team_send", args=other) is None

    # Durability: the journal row survives a reopen of the DB.
    conn.commit()
    with kb.connect() as c2:
        hit2 = store.op_lookup(c2, operation_id=None, verb="team_send", args=args)
        assert hit2 == '{"message_id": "5"}'


def test_op_create_team_is_dedupable(conn):
    """Unlike Phase 1's state["ops"] cache (which needed a team_id), the
    journal lets team_create dedup too — team_id is nullable for the
    not-yet-created team."""
    args = {"name": "alpha", "goal": "ship it"}
    assert store.op_lookup(conn, operation_id=None, verb="team_create", args=args) is None

    op_id = store.op_begin(conn, operation_id=None, team_id=None, verb="team_create", args=args)
    store.op_finish(conn, op_id, status="done", result_json='{"id": "t_root"}')

    hit = store.op_lookup(conn, operation_id=None, verb="team_create", args=args)
    assert hit == '{"id": "t_root"}'
