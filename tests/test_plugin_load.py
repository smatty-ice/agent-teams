"""register(ctx) smoke tests for the agent-teams plugin (Tasks 6 + 7).

A fake ``Ctx`` captures every ``register_tool`` / ``register_hook`` /
``register_cli_command`` call so we can assert ``register(ctx)`` wires up
exactly what the manifest promises — without booting the live plugin loader.

The fake ``Ctx`` method signatures MATCH the real ``PluginContext`` in
``hermes_cli/plugins.py``:

* ``register_tool(name, toolset, schema, handler, check_fn=None,
  requires_env=None, is_async=False, description="", emoji="",
  override=False)``  (line 317)
* ``register_hook(hook_name, callback)``  (line 936)
* ``register_cli_command(name, help, setup_fn, handler_fn=None,
  description="")``  (line 387)
"""
from __future__ import annotations


class FakeCtx:
    """Captures plugin registrations with the real PluginContext signatures."""

    manifest = type("M", (), {"name": "agent-teams", "key": "agent-teams"})()

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.hooks: list[tuple] = []
        self.cli_commands: list[dict] = []

    def register_tool(
        self,
        name,
        toolset,
        schema,
        handler,
        check_fn=None,
        requires_env=None,
        is_async=False,
        description="",
        emoji="",
        override=False,
    ) -> None:
        self.tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "check_fn": check_fn,
                "emoji": emoji,
            }
        )

    def register_hook(self, hook_name, callback) -> None:
        self.hooks.append((hook_name, callback))

    def register_cli_command(
        self, name, help, setup_fn, handler_fn=None, description=""
    ) -> None:
        self.cli_commands.append(
            {
                "name": name,
                "help": help,
                "setup_fn": setup_fn,
                "handler_fn": handler_fn,
            }
        )


def test_register_registers_17_team_tools(monkeypatch):
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    from plugins.agent_teams import register

    ctx = FakeCtx()
    register(ctx)

    team_tools = [t for t in ctx.tools if t["name"].startswith("team_")]
    assert len(team_tools) == 17, [t["name"] for t in ctx.tools]
    # Every team tool registers under the "team" toolset with an emoji + gate.
    for t in team_tools:
        assert t["toolset"] == "team"
        assert t["emoji"], t["name"]
        assert callable(t["check_fn"])
        assert callable(t["handler"])


def test_register_wires_pre_gateway_dispatch_hook(monkeypatch):
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    from plugins.agent_teams import register
    from plugins.agent_teams import watcher

    ctx = FakeCtx()
    register(ctx)

    hook_names = [h[0] for h in ctx.hooks]
    assert hook_names == ["pre_gateway_dispatch"], hook_names
    assert ctx.hooks[0][1] is watcher.run_passes_throttled


def test_register_wires_team_watch_cli_command(monkeypatch):
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    from plugins.agent_teams import register
    from plugins.agent_teams import watcher

    ctx = FakeCtx()
    register(ctx)

    cmd_names = [c["name"] for c in ctx.cli_commands]
    assert "team-watch" in cmd_names, cmd_names
    cmd = next(c for c in ctx.cli_commands if c["name"] == "team-watch")
    # handler_fn is the daemon; setup_fn must be callable (gets an argparse parser).
    assert cmd["handler_fn"] is watcher.watch_daemon
    assert callable(cmd["setup_fn"])
    assert cmd["help"]


def test_tool_handler_dispatches_to_correct_handler(monkeypatch):
    """The per-tool lambda must bind its own name (late-binding-closure guard)."""
    monkeypatch.setenv("HERMES_TEAM_LEAD", "1")
    from plugins.agent_teams import register
    from plugins.agent_teams import team_tools as tt

    ctx = FakeCtx()
    register(ctx)

    def _make_probe(_n):
        def _probe(args):
            return _n
        return _probe

    monkeypatch.setattr(
        tt, "HANDLERS", {name: _make_probe(name) for name in tt.HANDLERS}
    )
    for t in [t for t in ctx.tools if t["name"].startswith("team_")]:
        # The lambda must dispatch to the handler keyed by THIS tool's name,
        # not the last name in the loop (closure late-binding guard).
        result = t["handler"]({"probe": t["name"]})
        assert result == t["name"], (result, t["name"])
