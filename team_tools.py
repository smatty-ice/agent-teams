"""Team tools — Claude-parity agent-teams surface for Hermes.

Nine tools (`team_create`, `team_spawn`, `team_task_create`, `team_send`,
`team_inbox`, `team_inbox_ack`, `team_status`, `team_shutdown`,
`team_delete`) wrap the domain functions in :mod:`hermes_cli.kanban_team`.

Gated identically to `tools/kanban_tools.py`: visible only when
``HERMES_TEAM_LEAD=1`` is set in the environment, or when the active
profile lists ``team`` in its ``toolsets`` config (a YAML list, mirroring
the kanban-toolset gating convention).

Idempotency cache (plan-b's contribution in Phase-1 form): the four
mutating handlers that take a ``team_id`` (`team_spawn`,
`team_task_create`, `team_send`, `team_shutdown`) hash their input args
and stash the result JSON under ``state["ops"][hash]``. A duplicate call
with identical args returns the cached result instead of double-writing.
Full ``team_operations`` journal table is Phase 2.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from . import kanban_team as kt
# --- Phase 2 begin ---
from . import kanban_team_store as kts
from . import kanban_team_recovery as ktr
# --- Phase 2 end ---
from tools.registry import tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def _profile_has_team_toolset() -> bool:
    """Show team_* tools when env-gated OR when the profile opts in.

    Env gate: ``HERMES_TEAM_LEAD`` set to ``"1"``, ``"true"``, or
    ``"yes"`` (case-insensitive).

    Profile gate: ``toolsets:`` list in config contains ``team``.
    Matches the list-shape gating used by `_profile_has_kanban_toolset`
    in ``tools/kanban_tools.py``.
    """
    env = os.environ.get("HERMES_TEAM_LEAD", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "team" in toolsets
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn():
    kb.init_db()
    conn = kb.connect()
    from . import kanban_team_store as kts
    kts.ensure_tables(conn)
    return conn


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# --- Phase 2 begin ---
def _input_hash(verb: str, args: dict) -> str:
    """Deterministic short hash over (verb, sorted args).

    Delegates to the journal module so the team_operations table and any
    legacy reader hash identically. Kept as a thin shim under the original
    name so external callers/tests don't break.
    """
    return kts.op_input_hash(verb, args)


def _idempotent_lookup(conn, team_id: Optional[str], verb: str, args: dict) -> Optional[str]:
    """Return the journaled prior result-JSON for (verb, args), if any.

    Reads the durable ``team_operations`` journal first (Phase 2, Item 2),
    then falls back to Phase 1's per-team ``state["ops"]`` cache so a team
    created on the old code path still dedups after upgrade. ``team_id`` may
    be None (team_create dedups before the team exists — only the journal can
    answer that case).
    """
    try:
        hit = kts.op_lookup(conn, operation_id=None, verb=verb, args=args)
        if hit is not None:
            return hit
    except Exception:
        pass
    if not team_id:
        return None
    try:
        state = kt._read_state(conn, team_id)
        return (state.get("ops") or {}).get(_input_hash(verb, args))
    except Exception:
        return None


def _idempotent_remember(
    conn, team_id: Optional[str], verb: str, args: dict, result_json: str,
    *, status: str = "done", error: Optional[str] = None,
) -> None:
    """Persist the (verb, args) → result mapping in the durable journal.

    Replaces Phase 1's ``state["ops"]`` write with a ``team_operations``
    row (begin pending → finish terminal). ``status`` lets callers record a
    refusal (Item 5's plan-approval gate) as ``status="refused"`` rather than
    a successful result.
    """
    try:
        op_id = kts.op_begin(
            conn, operation_id=None, team_id=team_id, verb=verb, args=args,
        )
        kts.op_finish(
            conn, op_id, status=status, result_json=result_json, error=error,
        )
    except Exception:
        pass
# --- Phase 2 end ---


def _message_dict(m: kt.TeamMessage) -> dict[str, Any]:
    """Serialize a TeamMessage for tool JSON output.

    Uses ``from_sender`` (not the Python-keyword-colliding ``from``) so
    consumers don't have to worry about reserved-word lookups.
    """
    return {
        "id": m.id,
        "from_sender": m.from_sender,
        "body": m.body,
        "protocol_type": m.protocol_type,
        "ts": m.ts,
        "acked": m.acked,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_team_create(args: dict) -> str:
    name = (args.get("name") or "").strip()
    goal = (args.get("goal") or "").strip()
    board = args.get("board") or None
    if not name:
        return tool_error("name is required")
    if not goal:
        return tool_error("goal is required")
    try:
        conn = _conn()
        try:
            # --- Phase 2 begin ---
            # team_create is now dedupable via the journal (team_id is null
            # until the team exists, which the Phase 1 state["ops"] cache
            # could not handle).
            cached = _idempotent_lookup(conn, None, "team_create", args)
            if cached is not None:
                return cached
            rec = kt.team_create(conn, name=name, goal=goal, board=board)
            result = _ok(rec.as_dict())
            _idempotent_remember(conn, rec.id, "team_create", args, result)
            return result
            # --- Phase 2 end ---
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"team_create failed: {exc}")


def _handle_team_spawn(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    member_name = (args.get("member_name") or "").strip()
    role = (args.get("role") or "teammate").strip() or "teammate"
    profile = (args.get("profile") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    skills = args.get("skills") or None
    # --- Phase 2 begin ---
    agent_type = (args.get("agent_type") or "").strip() or None
    branch_name = (args.get("branch_name") or "").strip() or None
    # --- Phase 2 end ---
    if not team_id:
        return tool_error("team_id is required")
    if not member_name:
        return tool_error("member_name is required")
    if not profile:
        return tool_error("profile is required")
    if not prompt:
        return tool_error("prompt is required")
    try:
        conn = _conn()
        try:
            cached = _idempotent_lookup(conn, team_id, "team_spawn", args)
            if cached is not None:
                return cached
            member = kt.team_spawn(
                conn, team_id, member_name, role, profile, prompt,
                skills=skills,
                # --- Phase 2 begin ---
                agent_type=agent_type,
                branch_name=branch_name,
                # --- Phase 2 end ---
            )
            result = _ok(member.as_dict())
            _idempotent_remember(conn, team_id, "team_spawn", args, result)
            return result
        finally:
            conn.close()
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_spawn failed: {exc}")


def _handle_team_task_create(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    title = (args.get("title") or "").strip()
    body = args.get("body") or ""
    owner = args.get("owner") or None
    dependencies = args.get("dependencies") or []
    if not team_id:
        return tool_error("team_id is required")
    if not title:
        return tool_error("title is required")
    try:
        conn = _conn()
        try:
            cached = _idempotent_lookup(conn, team_id, "team_task_create", args)
            if cached is not None:
                return cached
            # --- Phase 2 begin ---
            try:
                task_id = kt.team_task_create(
                    conn, team_id, title, body=body,
                    owner=owner, dependencies=dependencies,
                )
            except kt.PlanApprovalRequired as exc:
                # Item 5: read-only gate. Record a refused op (not a result)
                # and surface the refusal — no task row was created.
                _idempotent_remember(
                    conn, team_id, "team_task_create", args, "",
                    status="refused", error=str(exc),
                )
                return tool_error(f"plan approval required: {exc}")
            result = _ok({"task_id": task_id})
            _idempotent_remember(conn, team_id, "team_task_create", args, result)
            return result
            # --- Phase 2 end ---
        finally:
            conn.close()
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_task_create failed: {exc}")


def _handle_team_send(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    to = (args.get("to") or "").strip()
    sender = (args.get("sender") or "lead").strip() or "lead"
    message = (args.get("message") or "").strip()
    protocol_type = args.get("protocol_type") or None
    if not team_id:
        return tool_error("team_id is required")
    if not to:
        return tool_error("to is required")
    if not message:
        return tool_error("message is required")
    try:
        conn = _conn()
        try:
            cached = _idempotent_lookup(conn, team_id, "team_send", args)
            if cached is not None:
                return cached
            msg_id = kt.team_send(
                conn, team_id,
                to=to, from_sender=sender, message=message,
                protocol_type=protocol_type,
            )
            result = _ok({"message_id": msg_id})
            _idempotent_remember(conn, team_id, "team_send", args, result)
            return result
        finally:
            conn.close()
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_send failed: {exc}")


def _handle_team_inbox(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    member = (args.get("member") or "").strip()
    unread_only = bool(args.get("unread_only", True))
    if not team_id:
        return tool_error("team_id is required")
    if not member:
        return tool_error("member is required")
    try:
        conn = _conn()
        try:
            msgs = kt.team_inbox(conn, team_id, member=member, unread_only=unread_only)
            return _ok([_message_dict(m) for m in msgs])
        finally:
            conn.close()
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_inbox failed: {exc}")


def _handle_team_inbox_ack(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    member = (args.get("member") or "").strip()
    through_id = args.get("through_id")
    if not team_id:
        return tool_error("team_id is required")
    if not member:
        return tool_error("member is required")
    if through_id is None or through_id == "":
        return tool_error("through_id is required")
    try:
        conn = _conn()
        try:
            kt.team_inbox_ack(
                conn, team_id, member=member, through_id=str(through_id),
            )
            return _ok({"acked": True, "member": member, "through_id": str(through_id)})
        finally:
            conn.close()
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_inbox_ack failed: {exc}")


def _handle_team_status(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return tool_error("team_id is required")
    try:
        conn = _conn()
        try:
            return _ok(kt.team_status(conn, team_id))
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"team_status failed: {exc}")


def _handle_team_shutdown(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    member = (args.get("member") or "").strip()
    sender = (args.get("sender") or "lead").strip() or "lead"
    reason = (args.get("reason") or "team shutdown").strip() or "team shutdown"
    try:
        timeout_seconds = float(args.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout_seconds = 0.0
    if not team_id:
        return tool_error("team_id is required")
    if not member:
        return tool_error("member is required")
    try:
        conn = _conn()
        try:
            cached = _idempotent_lookup(conn, team_id, "team_shutdown", args)
            if cached is not None:
                return cached
            msg_id = kt.team_shutdown(
                conn, team_id, member=member, sender=sender, reason=reason,
            )
            payload: dict[str, Any] = {
                "message_id": msg_id,
                "note": (
                    "shutdown_request sent. Poll team_inbox for the member's "
                    "shutdown_response. When the member's worker exits, the "
                    "auto-idle watcher marks it stopped automatically — no "
                    "manual status update is needed. To force a stop: call "
                    "team_shutdown again with timeout_seconds=N (hard "
                    "SIGTERM/SIGKILL + archive on timeout), or team_delete with "
                    "force=true (archives the member task)."
                ),
            }
            # Phase 3: when a positive grace window is given, escalate to a
            # hard shutdown if the member doesn't reply with shutdown_response
            # in time. The runtime helper waits, then SIGTERM/SIGKILL-reclaims
            # and archives the member task so the dispatcher can't re-spawn it.
            if timeout_seconds > 0:
                from . import kanban_team_runtime as ktr
                payload["hard_shutdown"] = ktr.team_member_hard_shutdown(
                    conn, team_id, member, timeout_seconds=timeout_seconds,
                )
            result = _ok(payload)
            _idempotent_remember(conn, team_id, "team_shutdown", args, result)
            return result
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"team_shutdown failed: {exc}")


def _handle_team_delete(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    force = bool(args.get("force", False))
    if not team_id:
        return tool_error("team_id is required")
    try:
        conn = _conn()
        try:
            kt.team_delete(conn, team_id, force=force)
            return _ok({"deleted": team_id})
        finally:
            conn.close()
    except RuntimeError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_delete failed: {exc}")


# --- Phase 2 begin ---
# Item 3 — debug bundle + export config.
def _handle_team_debug_bundle(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return tool_error("team_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.build_debug_bundle(conn, team_id))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_debug_bundle failed: {exc}")


def _handle_team_export_config(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return tool_error("team_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.export_config(conn, team_id))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_export_config failed: {exc}")


# Item 4 — curated recovery verbs.
def _handle_team_inspect(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return tool_error("team_id is required")
    task_id = (args.get("task_id") or "").strip() or None
    member = (args.get("member") or "").strip() or None
    operation_id = (args.get("operation_id") or "").strip() or None
    if not (task_id or member or operation_id):
        return tool_error("one of task_id, member, or operation_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.inspect(
                conn, team_id,
                task_id=task_id, member=member, operation_id=operation_id,
            ))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_inspect failed: {exc}")


def _handle_team_replay(args: dict) -> str:
    operation_id = (args.get("operation_id") or "").strip()
    if not operation_id:
        return tool_error("operation_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.replay(conn, operation_id))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_replay failed: {exc}")


def _handle_team_requeue(args: dict) -> str:
    message_id = args.get("message_id")
    if message_id is None or str(message_id).strip() == "":
        return tool_error("message_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.requeue(conn, int(message_id)))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_requeue failed: {exc}")


def _handle_team_mark_blocked(args: dict) -> str:
    task_id = (args.get("task_id") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not task_id:
        return tool_error("task_id is required")
    if not reason:
        return tool_error("reason is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.mark_blocked(conn, task_id, reason=reason))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_mark_blocked failed: {exc}")


def _handle_team_restore_assignment(args: dict) -> str:
    team_id = (args.get("team_id") or "").strip()
    task_id = (args.get("task_id") or "").strip()
    member = (args.get("member") or "").strip()
    if not team_id:
        return tool_error("team_id is required")
    if not task_id:
        return tool_error("task_id is required")
    if not member:
        return tool_error("member is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.restore_assignment(conn, team_id, task_id, member=member))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_restore_assignment failed: {exc}")


def _handle_team_explain_blockage(args: dict) -> str:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return tool_error("task_id is required")
    try:
        conn = _conn()
        try:
            return _ok(ktr.explain_blockage(conn, task_id))
        finally:
            conn.close()
    except (KeyError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"team_explain_blockage failed: {exc}")
# --- Phase 2 end ---


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_ARR_STR = {"type": "array", "items": {"type": "string"}}

SCHEMAS: dict[str, dict[str, Any]] = {
    "team_create": {
        "name": "team_create",
        "description": (
            "Create a new agent team. Returns the team_id (a Kanban root "
            "task id) plus the roster scaffold. The team root is sticky-"
            "blocked and stores roster/cursors/idempotency as prefixed "
            "JSON comments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {**_STR, "description": "Short slug for the team (no slashes/colons)."},
                "goal": {**_STR, "description": "One-paragraph statement of what this team should accomplish."},
                "board": {**_STR, "description": "Optional Kanban board slug. Defaults to the active board."},
            },
            "required": ["name", "goal"],
        },
    },
    "team_spawn": {
        "name": "team_spawn",
        "description": (
            "Register a named teammate and dispatch their initial task. "
            "Returns the TeamMember record. Idempotent by (team_id, member_name, role, profile, prompt, skills)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "member_name": {**_STR, "description": "Stable routing name for the teammate (e.g. 'researcher')."},
                "role": {**_STR, "description": "Human-readable role label."},
                "profile": {**_STR, "description": "Hermes profile to run the teammate as."},
                "prompt": {**_STR, "description": "Full instructions for the teammate's initial task."},
                "skills": {**_ARR_STR, "description": "Optional skills to load on the teammate's task."},
                "agent_type": {**_STR, "description": "Optional skill name to reuse as a subagent definition: its name is added to the task's skills and its SKILL.md body (frontmatter stripped) is appended to the prompt. Unknown names error."},
                "branch_name": {**_STR, "description": "Optional worktree branch for this teammate; collision-checked against live tasks (implies workspace_kind=worktree)."},
            },
            "required": ["team_id", "member_name", "role", "profile", "prompt"],
        },
    },
    "team_task_create": {
        "name": "team_task_create",
        "description": (
            "Create a work task scoped to a team. Returns the new task_id. "
            "Idempotent by (team_id, title, body, owner, dependencies)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "title": {**_STR, "description": "Short task title."},
                "body": {**_STR, "description": "Full task description and acceptance criteria."},
                "owner": {**_STR, "description": "Member name to assign the task to (resolves to that member's profile)."},
                "dependencies": {**_ARR_STR, "description": "task_ids that must complete before this task can claim."},
            },
            "required": ["team_id", "title"],
        },
    },
    "team_send": {
        "name": "team_send",
        "description": (
            "Send a direct message to a teammate by stable name. Returns "
            "the new message_id (a Kanban comment id, stringified). "
            "Idempotent by (team_id, to, sender, message, protocol_type)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "to": {**_STR, "description": "Recipient member name."},
                "sender": {**_STR, "description": "Sender identity (default: 'lead')."},
                "message": {**_STR, "description": "Message body."},
                "protocol_type": {
                    "type": "string",
                    "enum": [
                        "shutdown_request", "shutdown_response",
                        "plan_approval_request", "plan_approval_response",
                    ],
                    "description": "Optional protocol message type — typed handshakes the recipient is expected to respond to.",
                },
            },
            "required": ["team_id", "to", "message"],
        },
    },
    "team_inbox": {
        "name": "team_inbox",
        "description": (
            "Read messages addressed to a teammate. Returns a list of "
            "{id, from_sender, body, protocol_type, ts, acked}. "
            "`unread_only=true` (default) filters to messages newer than "
            "the member's stored cursor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "member": {**_STR, "description": "Member whose inbox to read."},
                "unread_only": {**_BOOL, "description": "If true (default), only return messages with id > the member's cursor."},
            },
            "required": ["team_id", "member"],
        },
    },
    "team_inbox_ack": {
        "name": "team_inbox_ack",
        "description": (
            "Advance a member's inbox cursor strictly monotonically. "
            "Cursors never move backwards; a stale ack is a no-op."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "member": {**_STR, "description": "Member whose cursor to advance."},
                "through_id": {**_STR, "description": "Comment id (as string) to acknowledge through (inclusive)."},
            },
            "required": ["team_id", "member", "through_id"],
        },
    },
    "team_status": {
        "name": "team_status",
        "description": (
            "Return a snapshot of the team: roster + work-task counts "
            "grouped by status. Roster includes status, role, profile, "
            "and task_id per member."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
            },
            "required": ["team_id"],
        },
    },
    "team_shutdown": {
        "name": "team_shutdown",
        "description": (
            "Send a cooperative shutdown_request protocol message to a "
            "teammate. The teammate replies with shutdown_response and, once "
            "its worker exits, the auto-idle watcher marks it stopped "
            "automatically. Set timeout_seconds=N for a hard shutdown: if no "
            "shutdown_response arrives within N seconds, the member's worker is "
            "SIGTERM/SIGKILL-reclaimed and its task archived."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "member": {**_STR, "description": "Member to shut down."},
                "sender": {**_STR, "description": "Sender identity (default: 'lead')."},
                "reason": {**_STR, "description": "Reason payload for the shutdown_request body."},
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "Optional grace window in seconds. When set and >0, after "
                        "sending the cooperative shutdown_request the lead waits up "
                        "to this long for the member's shutdown_response; if none "
                        "arrives the member's worker is hard-terminated (SIGTERM/"
                        "SIGKILL) and its task is archived so it can't re-spawn."
                    ),
                },
            },
            "required": ["team_id", "member"],
        },
    },
    "team_delete": {
        "name": "team_delete",
        "description": (
            "Archive the team root and (Phase-1 hard-shutdown win) each "
            "member task. Refuses while any member is in a non-terminal "
            "state — call team_shutdown first, or pass force=true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "force": {**_BOOL, "description": "Skip the active-member check."},
            },
            "required": ["team_id"],
        },
    },
    # --- Phase 2 begin ---
    # Item 3 — debug bundle + export config.
    "team_debug_bundle": {
        "name": "team_debug_bundle",
        "description": (
            "Dump a team's full durable state for triage/replay: team record, "
            "state snapshot, operations journal, messages, tasks, links, "
            "comments, events, runs, profiles, config, recent failure logs, "
            "and replay instructions. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
            },
            "required": ["team_id"],
        },
    },
    "team_export_config": {
        "name": "team_export_config",
        "description": (
            "Return a Claude-shaped team config JSON {name, goal, board, "
            "members:[...]} — the parity analog of "
            "~/.claude/teams/<name>/config.json. Returns the JSON; does not "
            "write a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
            },
            "required": ["team_id"],
        },
    },
    # Item 4 — curated recovery verbs.
    "team_inspect": {
        "name": "team_inspect",
        "description": (
            "Read-only deep view of one task, member, or journaled operation. "
            "Pass exactly one of task_id / member / operation_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "task_id": {**_STR, "description": "Inspect this task (with comments/events/runs)."},
                "member": {**_STR, "description": "Inspect this member's roster entry."},
                "operation_id": {**_STR, "description": "Inspect this journaled operation."},
            },
            "required": ["team_id"],
        },
    },
    "team_replay": {
        "name": "team_replay",
        "description": (
            "Reset a failed/refused journaled operation back to pending so the "
            "next identical tool call re-attempts it idempotently. A done op is "
            "left untouched."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation_id": {**_STR, "description": "operation_id from team_debug_bundle / team_inspect."},
            },
            "required": ["operation_id"],
        },
    },
    "team_requeue": {
        "name": "team_requeue",
        "description": (
            "Clear the dead_letter flag and reset delivered_at on a message so "
            "it is redelivered on the recipient's next inbox read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {**_STR, "description": "team_messages row id (the message's index id, not the comment id)."},
            },
            "required": ["message_id"],
        },
    },
    "team_mark_blocked": {
        "name": "team_mark_blocked",
        "description": (
            "Lead-initiated block of a team task with a reason (wraps Kanban "
            "block_task). The task transitions to blocked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {**_STR, "description": "Task to block."},
                "reason": {**_STR, "description": "Why it is being blocked."},
            },
            "required": ["task_id", "reason"],
        },
    },
    "team_restore_assignment": {
        "name": "team_restore_assignment",
        "description": (
            "Re-attach the [owner:@<member>] body header and assignee to an "
            "orphaned task whose ownership was lost."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {**_STR, "description": "team_id returned by team_create."},
                "task_id": {**_STR, "description": "Task to re-own."},
                "member": {**_STR, "description": "Member to re-attach as owner."},
            },
            "required": ["team_id", "task_id", "member"],
        },
    },
    "team_explain_blockage": {
        "name": "team_explain_blockage",
        "description": (
            "Read-only: report a task's unmet (non-done) dependency parents and "
            "its most recent failure, so a lead can see why it is parked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {**_STR, "description": "Task to explain."},
            },
            "required": ["task_id"],
        },
    },
    # --- Phase 2 end ---
}

HANDLERS = {
    "team_create":      _handle_team_create,
    "team_spawn":       _handle_team_spawn,
    "team_task_create": _handle_team_task_create,
    "team_send":        _handle_team_send,
    "team_inbox":       _handle_team_inbox,
    "team_inbox_ack":   _handle_team_inbox_ack,
    "team_status":      _handle_team_status,
    "team_shutdown":    _handle_team_shutdown,
    "team_delete":      _handle_team_delete,
    # --- Phase 2 begin ---
    "team_debug_bundle":  _handle_team_debug_bundle,
    "team_export_config": _handle_team_export_config,
    "team_inspect":             _handle_team_inspect,
    "team_replay":              _handle_team_replay,
    "team_requeue":             _handle_team_requeue,
    "team_mark_blocked":        _handle_team_mark_blocked,
    "team_restore_assignment":  _handle_team_restore_assignment,
    "team_explain_blockage":    _handle_team_explain_blockage,
    # --- Phase 2 end ---
}

EMOJIS = {
    "team_create":      "👥",
    "team_spawn":       "🚀",
    "team_task_create": "📋",
    "team_send":        "📨",
    "team_inbox":       "📬",
    "team_inbox_ack":   "✅",
    "team_status":      "📊",
    "team_shutdown":    "🛑",
    "team_delete":      "🗑",
    # --- Phase 2 begin ---
    "team_debug_bundle":  "🐞",
    "team_export_config": "📤",
    "team_inspect":             "🔍",
    "team_replay":              "🔁",
    "team_requeue":             "♻️",
    "team_mark_blocked":        "🚧",
    "team_restore_assignment":  "🔧",
    "team_explain_blockage":    "❓",
    # --- Phase 2 end ---
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Tool registration is performed by the plugin's ``register(ctx)`` entrypoint
# in ``__init__.py`` via ``ctx.register_tool(...)`` — the SCHEMAS / HANDLERS /
# EMOJIS tables above are the source of truth it loops over. (The module-level
# ``registry.register`` loop that lived here was removed when this moved from
# core ``tools/`` into the plugin; the plugin must not import the global
# ``tools.registry`` registry directly.)
