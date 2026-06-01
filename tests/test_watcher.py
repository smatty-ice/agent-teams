"""Watcher tests for the agent-teams plugin (Task 7).

``run_passes_throttled`` is a ``pre_gateway_dispatch`` callback. Its contract is
OBSERVER-ONLY: it must always return ``None`` (never altering dispatch) and must
run the auto-idle/push passes at most once per ``WATCH_INTERVAL`` seconds. It
must never raise out to the gateway loop.
"""
from __future__ import annotations


def test_run_passes_throttled_is_observer_and_throttled(monkeypatch):
    from plugins.agent_teams import watcher

    ticks = []
    monkeypatch.setattr(
        watcher, "_tick_all_boards", lambda **kw: ticks.append(1)
    )
    # Reset the module-level throttle state so the test is deterministic.
    watcher._last_run["t"] = 0.0

    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    # First call: throttle window is open (1000 - 0 >= WATCH_INTERVAL) -> ticks.
    r1 = watcher.run_passes_throttled(
        event=object(), gateway=object(), session_store=object(), now_fn=now
    )
    assert r1 is None
    assert len(ticks) == 1

    # Second call well within WATCH_INTERVAL -> throttled, no extra tick.
    clock["t"] = 1000.0 + watcher.WATCH_INTERVAL - 0.001
    r2 = watcher.run_passes_throttled(
        event=object(), gateway=object(), session_store=object(), now_fn=now
    )
    assert r2 is None
    assert len(ticks) == 1, "must not tick twice within WATCH_INTERVAL"

    # Past the interval -> ticks again.
    clock["t"] = 1000.0 + watcher.WATCH_INTERVAL + 0.001
    r3 = watcher.run_passes_throttled(
        event=object(), gateway=object(), session_store=object(), now_fn=now
    )
    assert r3 is None
    assert len(ticks) == 2


def test_run_passes_throttled_never_raises(monkeypatch):
    """A failing pass must be swallowed — the gateway loop must never see it."""
    from plugins.agent_teams import watcher

    def boom(**kw):
        raise RuntimeError("pass exploded")

    monkeypatch.setattr(watcher, "_tick_all_boards", boom)
    watcher._last_run["t"] = 0.0

    result = watcher.run_passes_throttled(now_fn=lambda: 10_000.0)
    assert result is None  # swallowed, observer contract upheld


def test_run_passes_throttled_accepts_arbitrary_gateway_kwargs(monkeypatch):
    """The gateway passes event/gateway/session_store; **kw absorbs extras so a
    future gateway kwarg can't break the hook."""
    from plugins.agent_teams import watcher

    monkeypatch.setattr(watcher, "_tick_all_boards", lambda **kw: None)
    watcher._last_run["t"] = 0.0
    result = watcher.run_passes_throttled(
        event=1, gateway=2, session_store=3, some_future_kwarg=4,
        now_fn=lambda: 99_999.0,
    )
    assert result is None


def test_tick_all_boards_runs_both_passes_per_board(monkeypatch):
    """_tick_all_boards must enumerate boards by slug, ensure tables, skip
    team-less boards, and run auto-idle then push on boards that have teams."""
    from plugins.agent_teams import watcher
    from plugins.agent_teams import kanban_team as kt
    from plugins.agent_teams import kanban_team_runtime as ktr
    from plugins.agent_teams import kanban_team_store as kts
    from hermes_cli import kanban_db as kb

    # Two boards; only "beta" has teams.
    monkeypatch.setattr(
        kb, "list_boards", lambda **kw: [{"slug": "default"}, {"slug": "beta"}]
    )

    connected = []

    class FakeConn:
        def __init__(self, slug):
            self.slug = slug

        def close(self):
            pass

    def fake_connect(*, board=None):
        c = FakeConn(board)
        connected.append(board)
        return c

    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kts, "ensure_tables", lambda c: None)
    monkeypatch.setattr(
        kt, "list_teams", lambda c: (["t"] if c.slug == "beta" else [])
    )

    idle_calls, push_calls = [], []
    monkeypatch.setattr(
        ktr, "run_auto_idle_pass", lambda c: idle_calls.append(c.slug)
    )
    monkeypatch.setattr(
        ktr, "run_push_delivery_pass", lambda c: push_calls.append(c.slug)
    )

    watcher._tick_all_boards()

    assert connected == ["default", "beta"]
    assert idle_calls == ["beta"]
    assert push_calls == ["beta"]
