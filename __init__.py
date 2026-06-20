"""agent-teams plugin — Claude-parity agent teams for Hermes.

All state rides on Kanban (kanban_db); the plugin's three tables are created
idempotently on connect (see kanban_team_store.ensure_tables). Tools register
via ctx.register_tool. Gated on HERMES_TEAM_LEAD=1 or `team` in profile toolsets.

The Phase-3 gateway watcher (auto-idle + push) is re-homed onto plugin-safe
surfaces (see watcher.py): a throttled ``pre_gateway_dispatch`` hook for active
use plus an opt-in ``hermes team-watch`` daemon for true timer delivery.
"""
import logging

from . import team_tools as tt

logger = logging.getLogger(__name__)


def _team_watch_setup(parser) -> None:
    """argparse setup_fn for the ``team-watch`` CLI command. The daemon takes
    no arguments today; defined so register_cli_command's required ``setup_fn``
    has something to attach the parser to (and a place to grow flags later)."""
    return None


def _warn_on_existing_team_tool(name: str) -> None:
    """AT-02: make a same-toolset registration collision visible.

    The core ``tools/team_tools.py`` (the built-in copy dispatcher-spawned
    workers load) registers the same 18 names under the same ``"team"`` toolset
    at import. ``tools.registry.register`` only rejects a *cross-toolset* shadow;
    a same-toolset re-registration is a **silent last-write-wins overwrite**
    (no error, no log). So before we register, check the registry and log a
    WARNING when ``name`` is already present under ``"team"`` — this plugin's
    registration will overwrite the built-in handler, and that swap should be
    auditable in agent.log rather than invisible. Best-effort: any failure to
    introspect the registry is swallowed so plugin load never breaks on it.

    The deeper fix (make the registry itself log same-toolset overwrites)
    belongs to the hermes-agent repo that owns ``tools/registry.py`` — see
    ``docs/RESTRUCTURE-single-source.md``.
    """
    try:
        from tools.registry import registry
        existing = registry.get_entry(name)
    except Exception:
        return
    if existing is not None and getattr(existing, "toolset", None) == "team":
        logger.warning(
            "agent-teams: tool %r is already registered under toolset 'team' "
            "(likely the built-in tools/team_tools.py); the plugin "
            "registration will overwrite it (same-toolset last-write-wins). "
            "This dual registration is AT-02 — see "
            "docs/RESTRUCTURE-single-source.md.",
            name,
        )


def register(ctx) -> None:
    """Plugin entrypoint. Registers the 18 team_* tools, the gateway watcher
    hook, and the ``team-watch`` CLI daemon command."""
    for name, schema in tt.SCHEMAS.items():
        _warn_on_existing_team_tool(name)
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
