"""Team hook registry + event pump (Phase 2 Item 7).

Claude's agent-teams expose ``TaskCreated`` / ``TaskCompleted`` /
``TeammateIdle`` hooks. Hermes has no ``VALID_HOOKS`` equivalent for these —
those are agent-session lifecycle hooks, unrelated to Kanban task events
(MASTER-PLAN substrate fact §0.5). So this is **net-new plumbing**: a
subscription registry (``team_hook_subs``) plus a cursor-paged pump over
``task_events``, modeled on the gateway's ``unseen_events_for_sub``
(``kanban_db.py``) — the canonical cursor-over-events precedent.

Each hook kind maps to one or more native Kanban event kinds. Events are
filtered to the subscribing team's tenant (``tasks.tenant = team:<name>``) via
a join, so a hook never sees a task from another team or an untenanted board
task. ``TeammateIdle`` has no native source: ``kanban_team.team_member_update``
emits a synthetic ``team_idle`` event on the team root when a member goes idle.

Owned by Phase 2; Phase 3 does not edit this module.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import Event


HOOK_KIND_TO_EVENTS: dict[str, list[str]] = {
    "TaskCreated": ["created"],
    "TaskCompleted": ["completed"],
    "TeammateIdle": ["team_idle"],
}
"""Map a Claude-style hook kind to the native Kanban event kind(s) it covers."""


def _team_tenant(conn: sqlite3.Connection, team_id: str) -> Optional[str]:
    """Resolve the tenant string (``team:<name>``) for a team root id."""
    row = conn.execute(
        "SELECT tenant FROM tasks WHERE id = ?", (team_id,)
    ).fetchone()
    return row["tenant"] if row is not None else None


def register_hook(
    conn: sqlite3.Connection, *, team_id: str, hook_kind: str,
) -> str:
    """Register a hook subscription and return its ``sub_id``.

    Raises :class:`ValueError` for an unknown ``hook_kind``. The cursor starts
    at the current max ``task_events.id`` for the team's tenant, so a freshly
    registered hook only sees events that arrive *after* registration (matching
    Claude's at-registration-time semantics).
    """
    if hook_kind not in HOOK_KIND_TO_EVENTS:
        raise ValueError(
            f"unknown hook_kind {hook_kind!r}; must be one of "
            f"{sorted(HOOK_KIND_TO_EVENTS)}"
        )
    tenant = _team_tenant(conn, team_id)
    # Start the cursor at the team's current max event id so prior events
    # aren't replayed into a brand-new subscription.
    start = 0
    if tenant is not None:
        row = conn.execute(
            "SELECT COALESCE(MAX(te.id), 0) AS m FROM task_events te "
            "JOIN tasks t ON te.task_id = t.id WHERE t.tenant = ?",
            (tenant,),
        ).fetchone()
        start = int(row["m"]) if row is not None else 0
    sub_id = "hook_" + secrets.token_hex(6)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO team_hook_subs "
            "(sub_id, team_id, hook_kind, last_event_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sub_id, team_id, hook_kind, start, now),
        )
    return sub_id


def _hook_row(conn: sqlite3.Connection, sub_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM team_hook_subs WHERE sub_id = ?", (sub_id,)
    ).fetchone()


def poll_hook(
    conn: sqlite3.Connection, sub_id: str, *, limit: Optional[int] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a hook subscription.

    Only events with ``id > last_event_id``, of this hook's mapped kind(s), and
    belonging to the subscribing team's tenant are returned. The cursor is NOT
    advanced here — call :func:`ack_hook` once the events have been handled
    (mirrors ``unseen_events_for_sub`` / ``advance_notify_cursor``).
    """
    row = _hook_row(conn, sub_id)
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    tenant = _team_tenant(conn, row["team_id"])
    if tenant is None:
        return cursor, []
    kinds = HOOK_KIND_TO_EVENTS.get(row["hook_kind"], [])
    if not kinds:
        return cursor, []

    placeholders = ",".join("?" * len(kinds))
    q = (
        "SELECT te.* FROM task_events te "
        "JOIN tasks t ON te.task_id = t.id "
        "WHERE t.tenant = ? AND te.id > ? "
        f"AND te.kind IN ({placeholders}) "
        "ORDER BY te.id ASC"
    )
    params: list[Any] = [tenant, cursor, *kinds]
    if limit is not None:
        q += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(q, params).fetchall()

    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def ack_hook(conn: sqlite3.Connection, sub_id: str, new_cursor: int) -> None:
    """Advance a hook's cursor monotonically (never backwards)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE team_hook_subs SET last_event_id = ? "
            "WHERE sub_id = ? AND last_event_id < ?",
            (int(new_cursor), sub_id, int(new_cursor)),
        )


def list_hooks(
    conn: sqlite3.Connection, *, team_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List hook subscriptions, optionally scoped to one team."""
    q = "SELECT * FROM team_hook_subs WHERE 1=1"
    params: list[Any] = []
    if team_id is not None:
        q += " AND team_id = ?"
        params.append(team_id)
    q += " ORDER BY created_at ASC"
    rows = conn.execute(q, params).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]
