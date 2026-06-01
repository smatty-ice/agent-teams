"""agent-teams plugin — Claude-parity agent teams for Hermes.

All state rides on Kanban (kanban_db); the plugin's three tables are created
idempotently on connect (see kanban_team_store.ensure_tables). Tools register
via ctx.register_tool. Gated on HERMES_TEAM_LEAD=1 or `team` in profile toolsets.

The Phase-3 gateway watcher (auto-idle + push) is re-homed onto plugin-safe
surfaces (see watcher.py): a throttled ``pre_gateway_dispatch`` hook for active
use plus an opt-in ``hermes team-watch`` daemon for true timer delivery.
"""
from . import team_tools as tt


def _team_watch_setup(parser) -> None:
    """argparse setup_fn for the ``team-watch`` CLI command. The daemon takes
    no arguments today; defined so register_cli_command's required ``setup_fn``
    has something to attach the parser to (and a place to grow flags later)."""
    return None


def register(ctx) -> None:
    """Plugin entrypoint. Registers the 17 team_* tools, the gateway watcher
    hook, and the ``team-watch`` CLI daemon command."""
    for name, schema in tt.SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="team",
            schema=schema,
            handler=(lambda args, _n=name, **kw: tt.HANDLERS[_n](args)),
            check_fn=tt._profile_has_team_toolset,
            emoji=tt.EMOJIS[name],
        )

    # Gateway watcher (Task 7). Imported lazily so the tool path doesn't pay
    # for the runtime/kanban_team imports unless register() runs.
    from . import watcher

    # Throttled auto-idle + push on gateway traffic (observer; returns None).
    ctx.register_hook("pre_gateway_dispatch", watcher.run_passes_throttled)

    # Opt-in true-timer daemon for idle-gateway delivery. The real
    # register_cli_command signature is (name, help, setup_fn, handler_fn=...,
    # description=...) — setup_fn gets the argparse subparser, handler_fn is
    # set as the dispatch func (called as handler_fn(args)).
    ctx.register_cli_command(
        name="team-watch",
        help="Run the agent-teams auto-idle + push watcher loop (blocking).",
        setup_fn=_team_watch_setup,
        handler_fn=watcher.watch_daemon,
        description=(
            "Blocking watcher that ticks every board with teams every "
            f"{int(watcher.WATCH_INTERVAL)}s, advancing roster auto-idle "
            "transitions and pushing addressed messages to recipients."
        ),
    )
