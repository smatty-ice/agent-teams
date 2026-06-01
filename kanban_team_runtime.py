"""Kanban Team v1 runtime — Phase 3 behaviors (MASTER-PLAN §8).

This module is the runtime sibling of :mod:`hermes_cli.kanban_team`. Where
``kanban_team`` holds the durable domain model (roster/mailbox/state on the
team root), this module holds the three *active* Phase-3 behaviors that run
either inside a tool handler (hard shutdown) or inside the gateway's
team-notifier coroutine (auto-idle + push delivery):

* :func:`team_member_hard_shutdown` — per-member hard shutdown with a grace
  window. Cooperative ``shutdown_response`` short-circuits; otherwise the
  worker is terminated via :func:`kanban_db.reclaim_task` (SIGTERM→SIGKILL)
  **and** the member task is :func:`kanban_db.archive_task`-d so the
  dispatcher can never re-spawn the stopped member (reclaim alone re-readies
  the task — MASTER-PLAN §8, planning-pass correctness rule).
* :func:`run_auto_idle_pass` — pollable watcher: a terminal member-task run
  event auto-transitions the roster status (``idle``, or ``stopped`` on
  archive), never reverting a deliberate stop, idempotent across passes.
* :func:`run_push_delivery_pass` — pollable watcher: a new addressed comment
  for member X re-readies X's task (:func:`wake_member`) so the dispatcher
  re-spawns X to process its inbox (true push, replacing lead-side polling).

Design constraints (Phase-3 merge contract):

* **No new SQLite tables, no new tool names.** Cursors (``auto_idle_cursor``,
  ``push_cursor``) live in the existing ``[team:state]`` JSON snapshot.
* **Does not edit** ``kanban_team.py`` / ``kanban_db.py`` / ``toolsets.py`` —
  it composes their public helpers read-only.
* **No gateway import** — pure domain so it stays unit-testable. The gateway
  wiring lives in :mod:`gateway.team_notifier`.
* **Injected clock + kill fn + event/wake fns** so unit tests never spawn a
  real subprocess or send a real signal.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from . import kanban_team as kt


# ---------------------------------------------------------------------------
# Item 1 — per-member hard shutdown with grace window
# ---------------------------------------------------------------------------

def team_member_hard_shutdown(
    conn: sqlite3.Connection,
    team_id: str,
    member: str,
    *,
    timeout_seconds: float,
    poll_interval: float = 1.0,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    signal_fn=None,
) -> dict[str, Any]:
    """Hard-shutdown one member with a cooperative grace window.

    Behavior:

    1. Resolve ``member`` from the roster (:class:`KeyError` if missing).
    2. **Grace loop** ``while now_fn() - start < timeout_seconds``: scan the
       lead's inbox for a ``shutdown_response`` from ``member``. On a hit →
       mark the member ``stopped`` and return ``{"outcome": "cooperative",
       "reclaimed": False, ...}`` *without* touching the worker process.
       Otherwise ``sleep_fn(poll_interval)`` and re-check.
    3. **On timeout** → :func:`kanban_db.reclaim_task` (SIGTERM→wait→SIGKILL
       via the injected ``signal_fn``, host-locality gated) and **then**
       :func:`kanban_db.archive_task` so the dispatcher cannot re-spawn the
       member (reclaim alone re-readies the task — MASTER-PLAN §8).
    4. Mark the member ``stopped`` and return ``{"outcome": "hard", ...}``.

    The clock (``now_fn``/``sleep_fn``), kill fn (``signal_fn``), are all
    injectable so unit tests run with zero real subprocesses/signals: a fake
    ``sleep_fn`` that advances ``now_fn`` past the timeout drives the whole
    grace loop synchronously.

    Returns a result dict: ``{"member", "outcome", "reclaimed", "archived",
    "task_id"}``.
    """
    team = kt.get_team(conn, team_id)
    member_rec = team.members.get(member)
    if member_rec is None:
        raise KeyError(
            f"team {team.name!r} has no member named {member!r}"
        )

    task_id = member_rec.task_id
    start = now_fn()

    # --- Grace loop: wait for a cooperative shutdown_response. -------------
    # We scan the LEAD's inbox (responses are addressed to the lead) for a
    # shutdown_response whose comment author is this member. unread_only=False
    # so we see the full history regardless of the lead's ack cursor.
    while now_fn() - start < timeout_seconds:
        if _member_acked_shutdown(conn, team_id, member):
            kt.team_member_update(conn, team_id, member, status="stopped")
            return {
                "member": member,
                "outcome": "cooperative",
                "reclaimed": False,
                "archived": False,
                "task_id": task_id,
            }
        sleep_fn(poll_interval)

    # --- Timeout → hard shutdown. ------------------------------------------
    reclaimed = False
    archived = False
    if task_id:
        reclaimed = kb.reclaim_task(
            conn, task_id,
            reason="team hard shutdown timeout",
            signal_fn=signal_fn,
        )
        # reclaim_task re-readies the task; archive it so the dispatcher
        # cannot re-spawn the stopped member (MASTER-PLAN §8 correctness rule).
        archived = kb.archive_task(conn, task_id)

    kt.team_member_update(conn, team_id, member, status="stopped")
    return {
        "member": member,
        "outcome": "hard",
        "reclaimed": reclaimed,
        "archived": archived,
        "task_id": task_id,
    }


def _member_acked_shutdown(
    conn: sqlite3.Connection, team_id: str, member: str,
) -> bool:
    """True if ``member`` has sent the lead a ``shutdown_response``.

    The lead's inbox holds messages addressed ``@lead:``; the sender is the
    comment author (no forgeable body prefix — MASTER-PLAN §2 ISS-6), so we
    match ``from_sender == member`` and ``protocol_type == "shutdown_response"``.
    """
    for msg in kt.team_inbox(conn, team_id, member="lead", unread_only=False):
        if (
            msg.from_sender == member
            and msg.protocol_type == "shutdown_response"
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Item 2 — auto-idle on dispatcher run completion
# ---------------------------------------------------------------------------

TERMINAL_EVENT_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "reclaimed", "archived",
)
"""Run-terminal ``task_events`` kinds emitted by the Kanban kernel
(MASTER-PLAN §8 substrate fact 4). A member-task event of one of these kinds
means the one-shot worker finished, so the roster member is no longer
``running`` and should be auto-transitioned."""

_STATUS_FOR_KIND = {
    "completed": "idle",
    "archived": "stopped",
}
"""Maps a terminal event kind to the roster status the member should take.
``archived`` is a hard stop (the task can't re-spawn), so the member becomes
``stopped``; every other terminal kind (the worker finished one run but the
task may be re-dispatched) lands the member back at ``idle`` (the default)."""


def run_auto_idle_pass(
    conn: sqlite3.Connection,
    *,
    events_fn: Callable[[sqlite3.Connection, str], Any] = kb.list_events,
) -> dict[str, Any]:
    """Auto-transition roster members on terminal member-task run events.

    For each team on the board, advance a per-team ``auto_idle_cursor`` over
    the (globally id-monotonic) ``task_events`` of its member tasks. A new
    event whose kind is in :data:`TERMINAL_EVENT_KINDS` transitions the
    member to :data:`_STATUS_FOR_KIND` (``idle`` by default, ``stopped`` on
    archive). Members already in a terminal roster status
    (:data:`kanban_team.MEMBER_STATUSES_TERMINAL`) are **skipped** so a
    deliberate stop is never reverted.

    The cursor lives in the existing ``[team:state]`` JSON snapshot (no new
    table). To avoid lost updates, the merged roster and the new cursor are
    written **once per team** at the end of the team's scan.

    Inert when no teams exist. ``events_fn`` is injectable so unit tests can
    feed a synthetic event stream without driving a real run.

    Returns ``{"teams": int, "transitions": int}``.
    """
    teams = kt.list_teams(conn)
    total_transitions = 0
    for team in teams:
        state = kt._read_state(conn, team.id)
        try:
            cursor = int(state.get("auto_idle_cursor", 0) or 0)
        except (TypeError, ValueError):
            cursor = 0

        roster = {n: m.as_dict() for n, m in team.members.items()}
        max_seen = cursor
        changed = False

        for name, member in team.members.items():
            if member.status in kt.MEMBER_STATUSES_TERMINAL:
                # Never revert a deliberate stop/done.
                continue
            if not member.task_id:
                continue
            for ev in events_fn(conn, member.task_id):
                if ev.id <= cursor or ev.kind not in TERMINAL_EVENT_KINDS:
                    continue
                if ev.id > max_seen:
                    max_seen = ev.id
                new_status = _STATUS_FOR_KIND.get(ev.kind, "idle")
                if roster[name].get("status") != new_status:
                    roster[name]["status"] = new_status
                    changed = True
                    total_transitions += 1

        if changed or max_seen != cursor:
            kt._update_state(
                conn, team.id, roster=roster, auto_idle_cursor=max_seen,
            )

    return {"teams": len(teams), "transitions": total_transitions}


# ---------------------------------------------------------------------------
# Item 3 — true push-based message delivery
# ---------------------------------------------------------------------------

def new_addressed_comments(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    since_id: int,
) -> list[tuple[int, str]]:
    """Return ``(comment_id, recipient)`` for addressed mailbox comments
    newer than ``since_id``.

    Scans the team root's comments and, for each comment with id > ``since_id``,
    tests it against every current roster member name using the existing
    :func:`kanban_team._parse_message_body` parser (so the ``@<name>:`` /
    ``[protocol]`` grammar stays single-sourced). ``[team:state]`` snapshots
    never match (they don't begin with ``@<name>:``) and are skipped.
    Recipients not in the roster (e.g. ``@lead:``) are not returned — the lead
    is a session, not a member task.
    """
    team = kt.get_team(conn, team_id)
    names = list(team.members.keys())
    out: list[tuple[int, str]] = []
    for comment in kb.list_comments(conn, team_id):
        if comment.id <= since_id:
            continue
        body = comment.body or ""
        for name in names:
            if kt._parse_message_body(body, recipient=name) is not None:
                out.append((comment.id, name))
                break
    return out


def wake_member(
    conn: sqlite3.Connection, team_id: str, member_name: str,
) -> bool:
    """Re-ready ``member_name``'s task so the dispatcher re-spawns it.

    On the one-shot-worker substrate, "push" = re-readying the recipient's
    task so the dispatcher picks it up and the worker processes its inbox.

    Guarded UPDATE inside a write transaction (same shape as
    :func:`kanban_db.reclaim_task`'s guarded resets): only re-readies a task
    that is **not** currently ``running`` / ``archived`` / ``done``, so we
    never resurrect an in-flight worker or a deliberately-stopped task.
    Returns True iff exactly one row was re-readied.
    """
    team = kt.get_team(conn, team_id)
    member = team.members.get(member_name)
    if member is None or not member.task_id:
        return False
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL "
            "WHERE id = ? AND status NOT IN ('running', 'archived', 'done')",
            (member.task_id,),
        )
        return cur.rowcount == 1


def run_push_delivery_pass(
    conn: sqlite3.Connection,
    *,
    wake_fn: Callable[[sqlite3.Connection, str, str], bool] = wake_member,
) -> dict[str, Any]:
    """Wake recipients of new addressed comments so the dispatcher re-spawns
    them to process their inbox (true push, replacing lead-side polling).

    For each team: read a per-team ``push_cursor`` from the ``[team:state]``
    snapshot, find :func:`new_addressed_comments` past it, call ``wake_fn``
    for each recipient that is a current roster member, and advance the cursor
    once at the end. The monotonic cursor makes re-runs no-ops; ``wake_member``
    is itself guarded so a running/done/archived recipient is never resurrected.

    Inert when no teams exist. ``wake_fn`` is injectable for unit tests.

    Returns ``{"teams": int, "woken": int}``.
    """
    teams = kt.list_teams(conn)
    total_woken = 0
    for team in teams:
        state = kt._read_state(conn, team.id)
        try:
            cursor = int(state.get("push_cursor", 0) or 0)
        except (TypeError, ValueError):
            cursor = 0

        max_seen = cursor
        for cid, recipient in new_addressed_comments(
            conn, team.id, since_id=cursor,
        ):
            if cid > max_seen:
                max_seen = cid
            if recipient not in team.members:
                continue
            if wake_fn(conn, team.id, recipient):
                total_woken += 1

        if max_seen != cursor:
            kt._update_state(conn, team.id, push_cursor=max_seen)

    return {"teams": len(teams), "woken": total_woken}
