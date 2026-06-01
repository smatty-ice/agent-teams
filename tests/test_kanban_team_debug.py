"""Tests for hermes_cli.kanban_team_recovery bundle assembly (Phase 2 Item 3):
team_debug_bundle + team_export_config.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from plugins.agent_teams import kanban_team as kt
from plugins.agent_teams import kanban_team_recovery as recovery

# The ``conn`` fixture (conftest.py) lays down core schema then the plugin's
# team_* tables via ensure_tables — these tables are no longer core schema.


def _seed_team(conn):
    rec = kt.team_create(conn, name="dbgteam", goal="ship the thing", board="b1")
    kt.team_spawn(conn, rec.id, "alice", "researcher", "p-alice", "do research")
    task_id = kt.team_task_create(conn, rec.id, "Write report", owner="alice")
    kt.team_send(conn, rec.id, to="alice", from_sender="lead", message="go")
    return rec, task_id


EXPECTED_BUNDLE_KEYS = {
    "team", "state", "operations", "messages", "tasks", "links",
    "comments", "events", "runs", "profiles", "config", "logs",
    "replay_instructions",
}


def test_team_debug_bundle_includes_all_sections(conn):
    rec, task_id = _seed_team(conn)
    bundle = recovery.build_debug_bundle(conn, rec.id)
    assert set(bundle.keys()) == EXPECTED_BUNDLE_KEYS
    # The team identity round-trips.
    assert bundle["team"]["name"] == "dbgteam"
    # Messages and tasks are populated.
    assert any(m["to_member"] == "alice" for m in bundle["messages"])
    assert any(t["id"] == task_id for t in bundle["tasks"])
    # Replay instructions are human-readable guidance, not empty.
    assert isinstance(bundle["replay_instructions"], str)
    assert bundle["replay_instructions"].strip()


def test_team_export_config_shape(conn):
    rec, _ = _seed_team(conn)
    cfg = recovery.export_config(conn, rec.id)
    assert cfg["name"] == "dbgteam"
    assert cfg["goal"] == "ship the thing"
    assert cfg["board"] == "b1"
    assert isinstance(cfg["members"], list)
    alice = next(m for m in cfg["members"] if m["name"] == "alice")
    assert alice["profile"] == "p-alice"
    assert alice["role"] == "researcher"
