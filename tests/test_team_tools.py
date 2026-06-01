"""Gating + registration tests for the plugin's team_tools.

Verifies the 17 team_* tools are hidden by default and become visible when the
lead-gate (``HERMES_TEAM_LEAD`` env var or ``team`` profile toolset) is on.

In the plugin world tools are registered via ``register(ctx)`` →
``ctx.register_tool`` → ``tools.registry.registry.register``, NOT by importing
``tools.team_tools`` at module load. These tests register the plugin's tools
into the real registry (mirroring what ``register()`` does), then resolve the
``team`` toolset through ``model_tools.get_tool_definitions`` — the same path
the live agent uses — so the gate behavior is exercised end-to-end.
"""
from __future__ import annotations

import pytest


# Phase 1 surface (the nine original tools).
PHASE1_TEAM_TOOLS = {
    "team_create", "team_spawn", "team_task_create",
    "team_send", "team_inbox", "team_inbox_ack",
    "team_status", "team_shutdown", "team_delete",
}

# Phase 2 additions: Item 3 (debug/export) + Item 4 (recovery verbs).
PHASE2_TEAM_TOOLS = {
    "team_debug_bundle", "team_export_config",
    "team_inspect", "team_replay", "team_requeue",
    "team_mark_blocked", "team_restore_assignment", "team_explain_blockage",
}

EXPECTED_TEAM_TOOLS = PHASE1_TEAM_TOOLS | PHASE2_TEAM_TOOLS


def _register_plugin_tools():
    """Register the plugin's team_* tools into the real registry, exactly as
    ``register(ctx)`` does (toolset='team', gated by _profile_has_team_toolset).

    Idempotent: re-registering the same name+toolset just overwrites the entry,
    so repeated calls across tests are safe.
    """
    from tools.registry import registry
    from plugins.agent_teams import team_tools as tt

    for name, schema in tt.SCHEMAS.items():
        registry.register(
            name=name,
            toolset="team",
            schema=schema,
            handler=(lambda args, _n=name: tt.HANDLERS[_n](args)),
            check_fn=tt._profile_has_team_toolset,
            emoji=tt.EMOJIS[name],
        )


def _team_names_in_team_toolset(monkeypatch, tmp_path):
    """Register the plugin tools, refresh the gate cache, and return the
    team_* names visible in the resolved ``team`` toolset."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)

    from model_tools import get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    _register_plugin_tools()
    invalidate_check_fn_cache()
    schemas = get_tool_definitions(enabled_toolsets=["team"], quiet_mode=True)
    names = {s["function"].get("name") for s in schemas if "function" in s}
    return {n for n in names if n and n.startswith("team_")}


def test_team_tools_hidden_without_env_or_profile(monkeypatch, tmp_path):
    """Without HERMES_TEAM_LEAD env (and no ``team`` toolset in profile),
    no ``team_*`` tool should resolve even when the team toolset is requested —
    the check_fn gate suppresses them.
    """
    monkeypatch.delenv("HERMES_TEAM_LEAD", raising=False)
    team_names = _team_names_in_team_toolset(monkeypatch, tmp_path)
    assert team_names == set(), f"team tools leaked without gate: {team_names}"


def test_team_tools_visible_with_env_var(monkeypatch, tmp_path):
    """With HERMES_TEAM_LEAD=1, all 17 team_* tools must be available."""
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    team_names = _team_names_in_team_toolset(monkeypatch, tmp_path)
    assert team_names == EXPECTED_TEAM_TOOLS, (
        f"expected {EXPECTED_TEAM_TOOLS}, got {team_names}"
    )


def test_register_provides_team_toolset(monkeypatch, tmp_path):
    """The plugin's ``register(ctx)`` is the discovery path (not a module-level
    loop). After register, the ``team`` toolset contains exactly the 17 tools.

    Mirrors the original ``test_team_tools_are_auto_discovered`` regression
    guard, retargeted to the plugin entrypoint: a regression where the tools
    fail to register (e.g. a missing SCHEMAS entry) would shrink this set.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    (tmp_path / ".hermes").mkdir(exist_ok=True)

    from plugins.agent_teams import register
    from plugins.agent_teams import team_tools as tt
    from model_tools import get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    # A minimal real-shaped ctx that routes register_tool to the real registry,
    # exactly like PluginContext does.
    from tools.registry import registry

    class _Ctx:
        manifest = type("M", (), {"name": "agent-teams", "key": "agent-teams"})()

        def register_tool(self, *, name, toolset, schema, handler,
                          check_fn=None, emoji="", **kw):
            registry.register(
                name=name, toolset=toolset, schema=schema, handler=handler,
                check_fn=check_fn, emoji=emoji,
            )

        def register_hook(self, *a, **k):
            pass

        def register_cli_command(self, *a, **k):
            pass

    register(_Ctx())
    invalidate_check_fn_cache()
    schemas = get_tool_definitions(enabled_toolsets=["team"], quiet_mode=True)
    names = {s["function"]["name"] for s in schemas if "function" in s}
    assert EXPECTED_TEAM_TOOLS.issubset(names)
    # All 17 SCHEMAS names registered.
    assert set(tt.SCHEMAS) == EXPECTED_TEAM_TOOLS


# ---------------------------------------------------------------------------
# Phase 3 Item 1 — team_shutdown timeout_seconds wiring
# ---------------------------------------------------------------------------

def test_team_shutdown_schema_has_timeout_seconds():
    """The Phase-3 wiring adds an optional `timeout_seconds` parameter to the
    `team_shutdown` schema (no new tool name)."""
    from plugins.agent_teams import team_tools as tt
    props = tt.SCHEMAS["team_shutdown"]["parameters"]["properties"]
    assert "timeout_seconds" in props
    assert props["timeout_seconds"]["type"] == "number"
    # Still optional — cooperative shutdown stays the default.
    assert "timeout_seconds" not in tt.SCHEMAS["team_shutdown"]["parameters"]["required"]


def test_team_shutdown_with_timeout_triggers_hard_shutdown(monkeypatch, tmp_path):
    """`team_shutdown(..., timeout_seconds=N)` sends the cooperative request
    AND invokes the hard-shutdown runtime; a member that has already replied
    with shutdown_response short-circuits to outcome=cooperative and ends
    `stopped` — with no real subprocess/signal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)

    from hermes_cli import kanban_db as kb
    from plugins.agent_teams import kanban_team as kt
    from plugins.agent_teams import kanban_team_store as kts
    try:
        kb._INITIALIZED_PATHS.clear()
    except AttributeError:
        pass
    kb.init_db()

    # Seed a team + member + a cooperative shutdown_response from the member.
    conn = kb.connect()
    try:
        kts.ensure_tables(conn)  # team_* tables aren't in core schema anymore
        team = kt.team_create(conn, name="alpha", goal="ship it")
        kt.team_spawn(conn, team.id, "worker", "teammate", "profile-a", "work")
        kt.team_send(
            conn, team.id, to="lead", from_sender="worker",
            message="ok", protocol_type="shutdown_response",
        )
        team_id = team.id
    finally:
        conn.close()

    import json
    from plugins.agent_teams import team_tools as tt
    out = tt._handle_team_shutdown({
        "team_id": team_id, "member": "worker", "timeout_seconds": 30,
    })
    payload = json.loads(out)

    # Cooperative request was still sent.
    assert "message_id" in payload
    # Hard-shutdown result is merged under "hard_shutdown".
    assert payload["hard_shutdown"]["outcome"] == "cooperative"

    conn = kb.connect()
    try:
        assert kt.get_team(conn, team_id).members["worker"].status == "stopped"
    finally:
        conn.close()
