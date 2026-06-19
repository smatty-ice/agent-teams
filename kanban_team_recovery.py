"""Recovery + debug tooling for agent teams (Phase 2 Items 3, 4).

Item 3 assembles a ``team_debug_bundle`` (a full dump of a team's durable
state for triage/replay) and ``team_export_config`` (a Claude-shaped config
JSON, the parity analog of ``~/.claude/teams/{name}/config.json``).

Item 4 adds the curated recovery verbs (inspect / replay / requeue /
mark_blocked / restore_assignment / explain_blockage) — read-mostly operations
a lead reaches for when a teammate's work stalls or a journaled op failed.

Owned by Phase 2; built on kanban_team / kanban_team_store / kanban_db. The
descoped verbs (``revoke_agent`` → use team_shutdown + team_delete(force);
``freeze_lane`` → no kernel primitive; ``escalate_to_orchestrator`` → no
orchestrator endpoint) are intentionally absent, documented in SKILL.md.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from . import kanban_team as kt
from . import kanban_team_store as kts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [
        {k: r[k] for k in r.keys()}
        for r in conn.execute(sql, params).fetchall()
    ]


def _team_task_ids(conn: sqlite3.Connection, tenant: str) -> list[str]:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE tenant = ?", (tenant,)
        ).fetchall()
    ]


REPLAY_INSTRUCTIONS = (
    "To replay a failed or pending operation: call team_replay with its "
    "operation_id (see the `operations` section). To redeliver a dead-lettered "
    "message: team_requeue with its message id (see `messages`). To re-attach "
    "an orphaned task's owner: team_restore_assignment. Inspect any single "
    "task/member/op with team_inspect. team_export_config emits a Claude-shaped "
    "team config you can diff against ~/.claude/teams/<name>/config.json."
)


# ---------------------------------------------------------------------------
# Item 3 — debug bundle + export config
# ---------------------------------------------------------------------------

def build_debug_bundle(conn: sqlite3.Connection, team_id: str) -> dict[str, Any]:
    """Assemble a full triage/replay bundle for a team.

    Returns a dict with these sections: ``team`` (record), ``state`` (latest
    snapshot), ``operations`` (journal), ``messages`` (mailbox index),
    ``tasks`` (every tenant task), ``links`` (dependency edges), ``comments``
    (root-card comments), ``events`` (tenant task events), ``runs`` (attempt
    history), ``profiles`` (per-member profile names), ``config``,
    ``logs`` (recent failure excerpts), and ``replay_instructions``.
    """
    team = kt.get_team(conn, team_id)
    tenant = kt._team_tenant(team.name)
    state = kt._read_state(conn, team_id)
    task_ids = _team_task_ids(conn, tenant)

    # Tasks in the tenant.
    tasks = _rows(conn, "SELECT * FROM tasks WHERE tenant = ? ORDER BY created_at ASC", (tenant,))

    # Dependency links touching any tenant task.
    links: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for tid in task_ids:
        links.extend(_rows(
            conn,
            "SELECT parent_id, child_id FROM task_links "
            "WHERE parent_id = ? OR child_id = ?",
            (tid, tid),
        ))
        runs.extend(_rows(conn, "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at ASC", (tid,)))
        events.extend(_rows(conn, "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (tid,)))

    # De-dupe links (a parent/child pair shared by two tenant tasks lists once).
    seen: set[tuple] = set()
    deduped_links: list[dict[str, Any]] = []
    for ln in links:
        key = (ln["parent_id"], ln["child_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped_links.append(ln)

    # Recent failure excerpts across tenant tasks.
    for t in tasks:
        err = t.get("last_failure_error")
        if err:
            logs.append({"task_id": t["id"], "last_failure_error": err})

    # Per-member profile names.
    profiles = {name: m.profile for name, m in team.members.items()}

    config = export_config(conn, team_id)

    return {
        "team": team.as_dict(),
        "state": state,
        "operations": kts.list_operations(conn, team_id=team_id),
        "messages": kts.list_messages(conn, team_id=team_id),
        "tasks": tasks,
        "links": deduped_links,
        "comments": [
            {"id": c.id, "author": c.author, "body": c.body, "created_at": c.created_at}
            for c in kb.list_comments(conn, team_id)
        ],
        "events": events,
        "runs": runs,
        "profiles": profiles,
        "config": config,
        "logs": logs,
        "replay_instructions": REPLAY_INSTRUCTIONS,
    }


def export_config(conn: sqlite3.Connection, team_id: str) -> dict[str, Any]:
    """Return a Claude-shaped team config dict (does NOT write a file).

    Shape: ``{name, goal, board, members: [{name, profile, role, status,
    task_id}, ...]}`` — the parity analog of Claude's
    ``~/.claude/teams/{name}/config.json``.
    """
    team = kt.get_team(conn, team_id)
    return {
        "name": team.name,
        "goal": team.goal,
        "board": team.board,
        "members": [
            {
                "name": m.name,
                "profile": m.profile,
                "role": m.role,
                "status": m.status,
                "task_id": m.task_id,
            }
            for m in team.members.values()
        ],
    }


# ---------------------------------------------------------------------------
# Item 4 — recovery verbs (curated 6)
# ---------------------------------------------------------------------------

def inspect(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    task_id: Optional[str] = None,
    member: Optional[str] = None,
    operation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Read-only deep view of one task, member, or journaled op.

    Exactly one of ``task_id`` / ``member`` / ``operation_id`` should be set;
    the matching section is returned (others are None). Raises
    :class:`KeyError` if the requested entity doesn't exist.
    """
    out: dict[str, Any] = {"team_id": team_id, "task": None, "member": None, "operation": None}
    if task_id is not None:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise KeyError(f"task {task_id!r} not found")
        out["task"] = {
            "task": {k: getattr(task, k) for k in (
                "id", "title", "status", "assignee", "tenant", "branch_name",
                "workspace_kind", "last_failure_error", "consecutive_failures",
            )},
            "comments": [
                {"id": c.id, "author": c.author, "body": c.body}
                for c in kb.list_comments(conn, task_id)
            ],
            "events": _rows(conn, "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)),
            "runs": _rows(conn, "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at ASC", (task_id,)),
        }
    if member is not None:
        team = kt.get_team(conn, team_id)
        m = team.members.get(member)
        if m is None:
            raise KeyError(f"team {team.name!r} has no member named {member!r}")
        out["member"] = m.as_dict()
    if operation_id is not None:
        op = kts.get_operation(conn, operation_id)
        if op is None:
            raise KeyError(f"operation {operation_id!r} not found")
        out["operation"] = op
    return out


def replay(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    """Re-run a ``failed``/``pending`` journaled op idempotently.

    This is a *read-and-reset* recovery primitive: it clears a stuck op back to
    ``pending`` so the next genuine tool invocation (with the same input hash)
    re-attempts it instead of short-circuiting on a cached result. A ``done`` op
    is left untouched (its result is the source of truth). Returns the op's
    current journal row.
    """
    op = kts.get_operation(conn, operation_id)
    if op is None:
        raise KeyError(f"operation {operation_id!r} not found")
    if op["status"] in ("failed", "refused"):
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE team_operations SET status = 'pending', error = NULL "
                "WHERE operation_id = ? AND status IN ('failed', 'refused')",
                (operation_id,),
            )
        op = kts.get_operation(conn, operation_id)
    return {"operation": op, "replayable": op["status"] in ("pending",)}


def requeue(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    """Clear ``dead_letter`` and reset ``delivered_at`` on a message row so it
    is redelivered on the recipient's next inbox read.

    Clearing the index flags is not sufficient on its own: ``unread_only`` inbox
    reads filter by the recipient's high-water comment cursor, so a message at or
    below the cursor stays hidden. We therefore also rewind the recipient's
    cursor to just before this message's comment, restoring standard
    high-water-mark recovery semantics (the message — and anything after it —
    becomes unread again).
    """
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE team_messages SET dead_letter = 0, delivered_at = NULL "
            "WHERE id = ?",
            (int(message_id),),
        )
        changed = int(cur.rowcount or 0)
    if changed == 0:
        raise KeyError(f"message {message_id!r} not found")
    row = conn.execute(
        "SELECT * FROM team_messages WHERE id = ?", (int(message_id),)
    ).fetchone()

    # Rewind the recipient's inbox cursor so unread_only reads resurface this
    # message (reconciles the team_messages index with the comment-cursor read
    # model — the audit's requeue/unread_only mismatch).
    team_id = row["team_id"]
    to_member = row["to_member"]
    comment_id = int(row["comment_id"])
    state = kt._read_state(conn, team_id)
    cursors = dict(state.get("cursors") or {})
    board = state.get("board")
    current = kt._read_cursor(cursors, board, to_member)
    target = comment_id - 1
    if current > target:
        cursors[kt._cursor_key(board, to_member)] = target
        kt._update_state(conn, team_id, cursors=cursors)
    return {k: row[k] for k in row.keys()}


def mark_blocked(
    conn: sqlite3.Connection, task_id: str, *, reason: str,
) -> dict[str, Any]:
    """Lead-initiated block of a team task with a reason (wraps kb.block_task)."""
    ok = kb.block_task(conn, task_id, reason=reason)
    return {"task_id": task_id, "blocked": bool(ok), "reason": reason}


def restore_assignment(
    conn: sqlite3.Connection,
    team_id: str,
    task_id: str,
    *,
    member: str,
) -> dict[str, Any]:
    """Re-attach the ``[owner:@<member>]`` body header + assignee to a task
    that lost its owner (e.g. a manual edit stripped the header)."""
    member = kt._require_slug(member, "member")
    team = kt.get_team(conn, team_id)
    m = team.members.get(member)
    if m is None:
        raise KeyError(f"team {team.name!r} has no member named {member!r}")
    task = kb.get_task(conn, task_id)
    if task is None:
        raise KeyError(f"task {task_id!r} not found")

    # Rebuild the body so its first line is the correct owner header.
    body = task.body or ""
    lines = body.split("\n")
    if lines and kt.OWNER_HEADER_RE.match(lines[0]):
        lines = lines[1:]
        # Drop a single blank separator line if present.
        if lines and not lines[0].strip():
            lines = lines[1:]
    new_body = f"{kt._owner_header(member)}\n\n" + "\n".join(lines)

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body = ?, assignee = ? WHERE id = ?",
            (new_body, m.profile, task_id),
        )
    return {"task_id": task_id, "owner": member, "assignee": m.profile}


def explain_blockage(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Read-only: report a task's unmet (non-done) dependency parents and its
    most recent failure, so a lead can see *why* a task is parked."""
    task = kb.get_task(conn, task_id)
    if task is None:
        raise KeyError(f"task {task_id!r} not found")

    parents = _rows(
        conn,
        "SELECT t.id AS id, t.title AS title, t.status AS status "
        "FROM task_links l JOIN tasks t ON l.parent_id = t.id "
        "WHERE l.child_id = ?",
        (task_id,),
    )
    unmet = [p for p in parents if p["status"] != "done"]
    last_run = conn.execute(
        "SELECT outcome, error, summary FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return {
        "task_id": task_id,
        "status": task.status,
        "unmet_dependencies": unmet,
        "last_failure_error": task.last_failure_error,
        "last_run": ({k: last_run[k] for k in last_run.keys()} if last_run else None),
    }
