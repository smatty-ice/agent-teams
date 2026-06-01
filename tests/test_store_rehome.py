"""Storage re-home tests for the agent-teams plugin (Tasks 3, 4, 5).

These verify the plugin's storage layer is self-contained — it must NOT depend
on core additions (``kb.list_comments_after`` or the ``team_*`` tables in core
schema). All three live in the plugin now.
"""
from hermes_cli import kanban_db as kb


# --- Task 3: list_comments_after re-homed into the plugin store -------------

def test_list_comments_after_pages_by_id(conn):
    from plugins.agent_teams import kanban_team_store as kts
    tid = kb.create_task(conn, title="[team:x]", created_by="t", initial_status="blocked")
    ids = [kb.add_comment(conn, tid, author="a", body=f"m{i}") for i in range(5)]
    got = kts.list_comments_after(conn, tid, after_id=ids[1])
    assert [c.id for c in got] == ids[2:]
    assert [c.id for c in kts.list_comments_after(conn, tid, after_id=0, limit=2)] == ids[:2]


# --- Task 4: team_* tables re-homed as idempotent ensure_tables -------------

def test_ensure_tables_is_idempotent(conn):
    from plugins.agent_teams import kanban_team_store as kts

    # Prove the PLUGIN's DDL — not core's leftover schema — creates all three
    # tables. This branch's core kanban_db still lays down team_* on init, so
    # DROP them first: only the plugin's ensure_tables can bring them back.
    with kb.write_txn(conn):
        for tbl in ("team_operations", "team_messages", "team_hook_subs"):
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    # Confirm the drop actually happened (otherwise the test proves nothing).
    present = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('team_operations','team_messages','team_hook_subs')"
        ).fetchall()
    }
    assert present == set(), f"tables not dropped: {present}"

    kts.ensure_tables(conn); kts.ensure_tables(conn)  # twice = no error

    # All three tables are back, created by the plugin's DDL.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(team_messages)")}
    assert {"delivered_at", "acked_at", "dead_letter"} <= cols
    assert conn.execute("SELECT 1 FROM team_operations LIMIT 0") is not None
    assert conn.execute("SELECT 1 FROM team_hook_subs LIMIT 0") is not None
    # And the dedup indexes (also part of the plugin DDL) were recreated.
    idx = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {"idx_team_ops_hash", "idx_team_msgs_box", "idx_team_hook_subs_team"} <= idx


# --- Task 5: _conn() auto-creates the tables on every open ------------------

def test_conn_autocreates_tables(tmp_path, monkeypatch):
    """A fresh _conn() must guarantee the plugin tables exist so handlers can
    INSERT immediately, without relying on core schema having created them.

    This branch's core kanban_db still creates the team_* tables (they are
    decommissioned later), so to prove the plugin is self-contained we DROP
    those tables on the live connection _conn() returns and verify the
    connection can still INSERT — i.e. ensure_tables ran inside _conn(),
    not core's init_db.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from plugins.agent_teams import team_tools as tt
    from plugins.agent_teams import kanban_team_store as kts

    # Spy: _conn() must call ensure_tables on the connection it returns.
    calls = []
    orig = kts.ensure_tables
    monkeypatch.setattr(kts, "ensure_tables", lambda c: (calls.append(c), orig(c))[1])

    conn = tt._conn()
    try:
        assert calls and calls[-1] is conn, "_conn() must call ensure_tables(conn)"
        # Drop the table core may have made, then re-run ensure_tables to prove
        # the plugin (not core) is what makes the INSERT succeed.
        conn.execute("DROP TABLE IF EXISTS team_operations")
        conn.commit()
        kts.ensure_tables(conn)
        conn.execute(
            "INSERT INTO team_operations "
            "(operation_id, team_id, verb, input_hash, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("op1", "team-x", "team_create", "deadbeef", "pending", 1, 1),
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM team_operations WHERE operation_id = 'op1'"
        ).fetchone() is not None
    finally:
        conn.close()
