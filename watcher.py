"""Re-homes the Phase-3 gateway watcher onto plugin-safe surfaces.

There is no plugin hook for a free-running gateway coroutine, so the auto-idle
+ push passes run on two surfaces:

  * ``run_passes_throttled()`` — a ``pre_gateway_dispatch`` callback that runs
    the passes at most once per :data:`WATCH_INTERVAL` seconds, on whatever
    board(s) have teams. OBSERVER ONLY: it always returns ``None`` (never
    influences dispatch) and never raises out to the gateway loop. Covers
    active team-lead use; will NOT fire on a fully idle gateway.
  * ``watch_daemon()`` — a ``hermes team-watch`` blocking loop for true 5s
    timer delivery when the gateway is idle.

Both delegate to ``_tick_all_boards()``, which enumerates boards via
``kb.list_boards()`` (returns metadata dicts — we read each ``slug``), ensures
the plugin tables exist, skips boards with no teams, and runs
``run_auto_idle_pass`` then ``run_push_delivery_pass``.
"""
import time

from hermes_cli import kanban_db as kb
from . import kanban_team as kt
from . import kanban_team_runtime as ktr
from . import kanban_team_store as kts

# Minimum seconds between watcher passes (Phase-3 default).
WATCH_INTERVAL = 5.0

# Module-level throttle state for the pre_gateway_dispatch path.
_last_run = {"t": 0.0}


def _tick_all_boards(now_fn=time.time):
    """Run one auto-idle + push pass across every board that has teams.

    ``kb.list_boards()`` returns a list of board-metadata dicts (each with a
    ``slug`` key), and always includes the default board, so there is always
    at least one entry. Boards with no teams are skipped cheaply (the passes
    are themselves inert without teams, but skipping avoids opening cursors).
    """
    boards = kb.list_boards() or []
    for meta in boards:
        slug = meta.get("slug") if isinstance(meta, dict) else meta
        conn = kb.connect(board=slug) if slug else kb.connect()
        try:
            kts.ensure_tables(conn)
            if not kt.list_teams(conn):
                continue
            ktr.run_auto_idle_pass(conn)
            ktr.run_push_delivery_pass(conn)
        finally:
            conn.close()


def run_passes_throttled(
    *, event=None, gateway=None, session_store=None, now_fn=time.time, **kw
):
    """``pre_gateway_dispatch`` callback. Observer only — always returns
    ``None`` (never alters dispatch). Throttled to once per
    :data:`WATCH_INTERVAL`.

    The gateway invokes this hook with ``event``, ``gateway`` and
    ``session_store`` keyword args (see ``gateway/run.py`` and
    ``hermes_cli/plugins.py`` VALID_HOOKS doc); ``**kw`` absorbs any future
    extras so a new gateway kwarg can never break the hook. ``now_fn`` is
    injectable for tests.
    """
    t = now_fn()
    if t - _last_run["t"] < WATCH_INTERVAL:
        return None
    _last_run["t"] = t
    try:
        _tick_all_boards(now_fn=now_fn)
    except Exception:
        # Observer contract: a misbehaving pass must never disrupt dispatch.
        pass
    return None


def watch_daemon(args=None):
    """``hermes team-watch`` — blocking true-timer loop.

    Registered as the CLI command's ``handler_fn``; argparse calls it as
    ``watch_daemon(args)`` with the parsed namespace (which we ignore).
    """
    while True:
        try:
            _tick_all_boards()
        except Exception:
            pass
        time.sleep(WATCH_INTERVAL)
