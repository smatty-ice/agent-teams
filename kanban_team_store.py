"""Durable message + operation journal layer for agent teams (Phase 2).

This module backs two Phase-2 storage concerns that the Phase-1 design kept
inside JSON-prefixed comments on the team root:

* **Operation journal** (Item 2) — ``team_operations`` rows give durable,
  cross-tool, restart-surviving idempotency. It promotes Phase 1's
  ``state["ops"][hash]`` cache (which lived per-team and could not dedup
  ``team_create``, since the team didn't exist yet). Dedup key is
  ``(verb, input_hash)`` unless the caller pins an explicit ``operation_id``.

* **Message index** (Item 1) — ``team_messages`` rows parallel the addressed
  ``task_comment`` audit record with ``delivered_at`` / ``acked_at`` /
  ``dead_letter`` columns. The comment (and its ``commented`` event) stay the
  source-of-truth signal Phase 3's notifier polls; these rows are an
  additional index, never a replacement.

Both tables are owned by Phase 2; Phase 3 does not edit this module or the
tables it manages.

Design precedent: ``hermes_cli/kanban_db.py`` (write_txn, hashing patterns)
and ``hermes_cli/kanban_team.py`` (the team domain layer this complements).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import Comment


# ---------------------------------------------------------------------------
# Comment paging (re-homed from core)
# ---------------------------------------------------------------------------

def list_comments_after(conn, task_id, after_id=0, limit=None):
    """id-ordered comment paging (re-homed from core; the plugin must not
    depend on kb.list_comments_after, which exists only on the core branch)."""
    q = ("SELECT * FROM task_comments WHERE task_id = ? AND id > ? ORDER BY id ASC")
    params = [task_id, int(after_id)]
    if limit is not None:
        q += " LIMIT ?"; params.append(int(limit))
    rows = conn.execute(q, params).fetchall()
    return [Comment(id=r["id"], task_id=r["task_id"], author=r["author"],
                    body=r["body"], created_at=r["created_at"]) for r in rows]


# ---------------------------------------------------------------------------
# Schema (re-homed from core; the plugin owns these tables now)
# ---------------------------------------------------------------------------

_DDL = [
    """CREATE TABLE IF NOT EXISTS team_operations (
        operation_id TEXT PRIMARY KEY, team_id TEXT, verb TEXT NOT NULL,
        input_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
        error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL, board TEXT,
        comment_id INTEGER NOT NULL, to_member TEXT NOT NULL, from_sender TEXT NOT NULL,
        protocol_type TEXT, body TEXT NOT NULL, created_at INTEGER NOT NULL,
        delivered_at INTEGER, acked_at INTEGER, dead_letter INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS team_hook_subs (
        sub_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, hook_kind TEXT NOT NULL,
        last_event_id INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)""",
    # One authoritative state row per team (roster, inbox/push cursors, ...).
    # Replaces the append-only ``[team:state]`` comment snapshots, which grew
    # unboundedly and were read back by created_at (1s granularity) with a
    # non-atomic read-modify-write -> lost updates + same-second ambiguity.
    """CREATE TABLE IF NOT EXISTS team_state (
        team_id TEXT PRIMARY KEY, state_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_team_ops_hash ON team_operations(input_hash)",
    "CREATE INDEX IF NOT EXISTS idx_team_ops_team ON team_operations(team_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_team_msgs_box ON team_messages(team_id, to_member, id)",
    "CREATE INDEX IF NOT EXISTS idx_team_hook_subs_team ON team_hook_subs(team_id, hook_kind)",
]


def ensure_tables(conn) -> None:
    """Idempotently create the plugin's tables. Called by _conn() on every
    open (cheap; CREATE ... IF NOT EXISTS is a no-op once present)."""
    with kb.write_txn(conn):
        for stmt in _DDL:
            conn.execute(stmt)


# ---------------------------------------------------------------------------
# Team state (single authoritative row per team)
# ---------------------------------------------------------------------------

def state_read(conn: sqlite3.Connection, team_id: str) -> Optional[dict[str, Any]]:
    """Return the team's state dict, or None if no row exists yet.

    Plain SELECT — safe to call inside or outside an open transaction.
    Returns None (not {}) so callers can distinguish "no row" (fall back to
    legacy comment snapshots for back-compat) from "empty state".
    """
    row = conn.execute(
        "SELECT state_json FROM team_state WHERE team_id = ?", (str(team_id),)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["state_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def state_write(conn: sqlite3.Connection, team_id: str, state: dict[str, Any]) -> None:
    """Upsert the single state row for ``team_id``.

    MUST be called inside an open ``kb.write_txn`` (the caller owns the txn so a
    read-modify-write can be atomic in one BEGIN IMMEDIATE — that serialization
    is what removes the lost-update race the comment snapshots had).
    """
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    now = int(time.time())
    conn.execute(
        "INSERT INTO team_state (team_id, state_json, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(team_id) DO UPDATE SET "
        "state_json = excluded.state_json, updated_at = excluded.updated_at",
        (str(team_id), payload, now),
    )


# ---------------------------------------------------------------------------
# Retention (bound growth for always-on teams)
# ---------------------------------------------------------------------------

def prune_history(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 86400,
) -> dict[str, int]:
    """Delete aged, terminal bookkeeping rows so the journal/mailbox tables do
    not grow without bound on a long-lived team. Conservative by design:

    - ``team_operations``: only ``status != 'pending'`` rows older than the
      cutoff (a still-pending op is never pruned).
    - ``team_messages``: only ``acked_at IS NOT NULL`` rows older than the
      cutoff (unacked and dead-lettered messages are kept regardless of age).

    ``team_state`` is one row per team and needs no pruning. Returns the delete
    counts. Not auto-run — call from a maintenance cron or the lead so the
    cadence/retention window is an explicit operational choice.
    """
    cutoff = int(time.time()) - int(older_than_seconds)
    with kb.write_txn(conn):
        ops = conn.execute(
            "DELETE FROM team_operations "
            "WHERE status != 'pending' AND updated_at < ?",
            (cutoff,),
        ).rowcount
        msgs = conn.execute(
            "DELETE FROM team_messages "
            "WHERE acked_at IS NOT NULL AND acked_at < ?",
            (cutoff,),
        ).rowcount
    return {
        "operations_deleted": int(ops or 0),
        "messages_deleted": int(msgs or 0),
    }


# ---------------------------------------------------------------------------
# Operation journal (Item 2)
# ---------------------------------------------------------------------------

def op_input_hash(verb: str, args: dict[str, Any]) -> str:
    """Deterministic short hash over ``(verb, sorted args)``.

    Moved verbatim from ``team_tools._input_hash`` so the journal and any
    legacy caller hash identically (sha256 hexdigest, first 16 chars).
    """
    payload = json.dumps(
        {"verb": verb, "args": args}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _derive_operation_id(
    operation_id: Optional[str], verb: str, input_hash: str
) -> str:
    """Resolve the journal primary key.

    When the caller supplies no ``operation_id`` we derive it from the input
    hash so the dedup key is ``(verb, input_hash)`` (the ``verb`` prefix keeps
    two different tools that happen to hash an identical arg dict distinct).
    We deliberately do NOT expose an ``operation_id`` tool parameter — the
    public tool signatures are frozen (plan §Hard rules / frozen signatures).
    """
    if operation_id:
        return str(operation_id)
    return f"{verb}:{input_hash}"


def op_lookup(
    conn: sqlite3.Connection,
    *,
    operation_id: Optional[str],
    verb: str,
    args: dict[str, Any],
) -> Optional[str]:
    """Return the journaled ``result_json`` for a completed prior op, or None.

    A hit is only returned for a ``status='done'`` row — pending/failed/refused
    rows do not short-circuit a fresh attempt (recovery verbs replay those).
    """
    input_hash = op_input_hash(verb, args)
    op_id = _derive_operation_id(operation_id, verb, input_hash)
    row = conn.execute(
        "SELECT result_json FROM team_operations "
        "WHERE operation_id = ? AND status = 'done'",
        (op_id,),
    ).fetchone()
    if row is not None and row["result_json"] is not None:
        return str(row["result_json"])
    return None


def op_begin(
    conn: sqlite3.Connection,
    *,
    operation_id: Optional[str],
    team_id: Optional[str],
    verb: str,
    args: dict[str, Any],
) -> str:
    """Record (or refresh) a ``pending`` journal row and return its id.

    Idempotent on the primary key: re-beginning the same operation leaves any
    prior terminal row in place (we use ``INSERT OR IGNORE`` then bump
    ``updated_at`` only when still pending) so a replay never clobbers a
    recorded result.
    """
    input_hash = op_input_hash(verb, args)
    op_id = _derive_operation_id(operation_id, verb, input_hash)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO team_operations "
            "(operation_id, team_id, verb, input_hash, status, "
            " result_json, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)",
            (op_id, team_id, verb, input_hash, now, now),
        )
    return op_id


def op_finish(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    status: str,
    result_json: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Mark a journal row terminal: ``done`` | ``failed`` | ``refused``.

    Only advances a row that is still ``pending`` (a recorded ``done`` is
    never overwritten), so a duplicate finish is harmless.
    """
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE team_operations "
            "SET status = ?, result_json = ?, error = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = 'pending'",
            (str(status), result_json, error, now, str(operation_id)),
        )


def get_operation(
    conn: sqlite3.Connection, operation_id: str
) -> Optional[dict[str, Any]]:
    """Return a journal row as a dict, or None. Used by recovery verbs."""
    row = conn.execute(
        "SELECT * FROM team_operations WHERE operation_id = ?",
        (str(operation_id),),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def list_operations(
    conn: sqlite3.Connection,
    *,
    team_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """List journal rows, newest first. Filters are optional and ANDed."""
    q = "SELECT * FROM team_operations WHERE 1=1"
    params: list[Any] = []
    if team_id is not None:
        q += " AND team_id = ?"
        params.append(team_id)
    if status is not None:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY created_at DESC, operation_id DESC"
    if limit is not None:
        q += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(q, params).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# Message index (Item 1)
# ---------------------------------------------------------------------------

def insert_message(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    comment_id: int,
    to_member: str,
    from_sender: str,
    body: str,
    protocol_type: Optional[str] = None,
    board: Optional[str] = None,
    dead_letter: bool = False,
) -> int:
    """Insert a ``team_messages`` row paralleling an addressed comment.

    Returns the new message row id. ``comment_id`` is the audit comment the
    domain layer already wrote (and which fired the ``commented`` event).
    """
    now = int(time.time())
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO team_messages "
            "(team_id, board, comment_id, to_member, from_sender, "
            " protocol_type, body, created_at, delivered_at, acked_at, dead_letter) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                team_id, board, int(comment_id), to_member, from_sender,
                protocol_type, body, now, 1 if dead_letter else 0,
            ),
        )
        return int(cur.lastrowid or 0)


def list_messages(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    to_member: Optional[str] = None,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    """List message-index rows for a team, id-ordered ascending."""
    q = "SELECT * FROM team_messages WHERE team_id = ? AND id > ?"
    params: list[Any] = [team_id, int(after_id)]
    if to_member is not None:
        q += " AND to_member = ?"
        params.append(to_member)
    q += " ORDER BY id ASC"
    rows = conn.execute(q, params).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def stamp_delivered(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    to_member: str,
    comment_ids: list[int],
) -> None:
    """Stamp ``delivered_at`` (once) on message rows for these comment ids.

    Idempotent: only rows with ``delivered_at IS NULL`` are touched, so the
    first read wins and a re-read does not move the timestamp.
    """
    if not comment_ids:
        return
    now = int(time.time())
    placeholders = ",".join("?" * len(comment_ids))
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE team_messages SET delivered_at = ? "
            "WHERE team_id = ? AND to_member = ? AND delivered_at IS NULL "
            f"AND comment_id IN ({placeholders})",
            [now, team_id, to_member, *[int(c) for c in comment_ids]],
        )


def stamp_acked_through(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    to_member: str,
    through_comment_id: int,
) -> None:
    """Stamp ``acked_at`` on rows whose ``comment_id`` <= ``through_comment_id``.

    Mirrors the cursor advance in ``team_inbox_ack``. Idempotent on
    ``acked_at IS NULL``.
    """
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE team_messages SET acked_at = ? "
            "WHERE team_id = ? AND to_member = ? AND acked_at IS NULL "
            "AND comment_id <= ?",
            (now, team_id, to_member, int(through_comment_id)),
        )


def dead_letter_for_stopped(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    member: str,
) -> int:
    """Mark all undelivered messages addressed to ``member`` dead-letter.

    Called when a member transitions to a terminal status (``stopped``) or the
    team is deleted: messages that were never delivered can't be, so flag them
    so a recovery verb can requeue them deliberately. Returns the row count.
    """
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE team_messages SET dead_letter = 1 "
            "WHERE team_id = ? AND to_member = ? AND delivered_at IS NULL "
            "AND dead_letter = 0",
            (team_id, member),
        )
        return int(cur.rowcount or 0)
