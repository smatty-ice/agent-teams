"""Kanban Team v1: agent-team coordination on top of Kanban.

This module is the team-flavored sibling of :mod:`hermes_cli.kanban_swarm`.
Where ``kanban_swarm`` lays down a fixed planning → workers → verifier →
synthesizer DAG, this module models the open-ended "lead with named
teammates" shape Claude Code's agent-teams feature exposes.

Like ``kanban_swarm`` it does **not** introduce a second scheduler. All
durability rides on the existing Kanban kernel:

  team root (status='blocked', sticky-blocked registry card, tenant=team:<name>)
      member task A   (assignee=profile-a, tenant=team:<name>, stand-alone)
      member task B   (assignee=profile-b, tenant=team:<name>, stand-alone)
      work tasks      (created via team_task_create; deps chain via parents)

Every team task carries ``tenant=f"team:{name}"`` so the indexed
``tasks(tenant)`` column (``kanban_db.py`` ``idx_tasks_tenant``) gives
O(log n) team-task enumeration for ``team_status`` / ``team_delete``
without a parent-link graph or a body ``LIKE`` scan
(MASTER-PLAN §2.6, plan-a-reviewer P1).

Two comment conventions live on the team root:

* ``[team:state] {<json>}`` — full state snapshots (roster, per-member
  inbox cursors, idempotency cache). Last write wins on a per-key basis,
  same merge model ``kanban_swarm.latest_blackboard`` uses for
  ``[swarm:blackboard]`` comments.
* ``@<recipient>: [<protocol_type>] <body>`` — addressed mailbox
  messages. The comment id is the message id; the sender is the comment
  *author* (no ``[from:X]`` body prefix — MASTER-PLAN §2 ISS-6), so sender
  authority can never be forged in the body.

Per-task ownership is recorded with a first-line body header
``[owner:@<member>]`` (sentinel ``-`` for unassigned) so two members
sharing a profile remain distinguishable (MASTER-PLAN §2.3, ISS-2).

Design precedent: ``hermes_cli/kanban_swarm.py``.
Substrate guide: ``docs/plans-eval/MASTER-PLAN.md`` §2 (storage decisions),
§5 (substrate facts).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from . import kanban_team_store as kts


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_PREFIX = "[team:state] "
"""Comment-body prefix for state-snapshot rows on the team root task."""

TEAM_TITLE_PREFIX = "[team:"
"""Task-title prefix for team cards. The root is ``[team:<name>]``; member
registry cards are ``[team:<name>:<member>] <role>``. Used by
:func:`list_teams` (``WHERE title LIKE '[team:%'``) and to exclude registry
cards from work-task counts in :func:`team_status`."""

OWNER_UNASSIGNED = "-"
"""Owner sentinel in the ``[owner:@-]`` body header for unassigned tasks."""

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
"""Team/member name rule (MASTER-PLAN §2.7, plan-a-reviewer F6): ASCII
alphanumerics plus hyphen, must start alphanumeric. Strict enough to defang
every SQL-LIKE wildcard (``%``, ``_``), path metacharacter (``/``, ``\\``,
``.``), and ID delimiter (``:``, ``]``, ``[``) the module relies on."""

OWNER_HEADER_RE = re.compile(r"^\[owner:@([A-Za-z0-9][A-Za-z0-9-]*|-)\]")
"""Parses the first-line ``[owner:@<member>]`` body header. Group 1 is the
member name, or ``-`` for unassigned."""

MEMBER_STATUSES_TERMINAL = frozenset({"stopped", "done"})
"""Member statuses that count as "no longer active" for
:func:`team_delete`'s active-member refusal."""

_VALID_PROTOCOL_TYPES = frozenset({
    "shutdown_request", "shutdown_response",
    "plan_approval_request", "plan_approval_response",
})

_IDENTITY_BOUND_PROTOCOL_TYPES = frozenset({
    "shutdown_response", "plan_approval_response",
})
"""Protocol message types whose ``from_sender`` is load-bearing for a trust
decision the *recipient* makes automatically (AT-03): a ``shutdown_response``
satisfies :func:`kanban_team_runtime._member_acked_shutdown` and short-circuits
a member's hard-shutdown grace window; a ``plan_approval_response`` satisfies
:func:`_has_plan_approval_response` and lifts the Item-5 read-only gate. Because
the comment author is whatever the caller passes, a worker could otherwise post
one of these *attributed to another member*. When the caller is a
dispatcher-spawned worker (its identity is provable from the trusted
``HERMES_KANBAN_TASK`` env the dispatcher set), :func:`team_send` requires
``from_sender`` to equal that worker's own member name for these types.
The cooperative *request* types (``shutdown_request`` / ``plan_approval_request``)
are not bound — those are lead→member directives, not self-asserted acks."""

WORKER_TASK_ENV = "HERMES_KANBAN_TASK"
"""Env var the dispatcher sets to the worker's own member-task id on every
spawned worker (see ``hermes_cli.kanban_db`` worker spawn). It is set by the
trusted dispatcher, never by the model, so it is the authority anchor for
"which member is this calling worker" — the same anchor ``tools/kanban_tools``
and ``tools/send_message_tool`` already use to gate worker-scoped actions."""


class PlanApprovalRequired(Exception):
    """Raised when a mutating team op targets a member still gated on plan
    approval (``requires_plan_approval=True`` in the roster) and no
    ``plan_approval_response`` involving that member has been recorded yet
    (Item 5). The tool layer maps this to a ``tool_error`` + a journaled
    ``status="refused"`` operation rather than a write."""


def _team_tenant(team_name: str) -> str:
    """Single source of truth for the per-team ``tenant`` string.

    Never construct ``f"team:{name}"`` anywhere else — go through here so the
    format stays consistent between writes and indexed reads.
    """

    return f"team:{team_name}"


def _owner_header(owner: Optional[str]) -> str:
    """Render the first-line ownership header for a team task body."""

    return f"[owner:@{owner or OWNER_UNASSIGNED}]"


CURSOR_DEFAULT_BOARD = "default"
"""Board token used in a cursor key when the team has no explicit board
(``board=None`` ⇒ the active/default Kanban board)."""


def _cursor_key(board: Optional[str], member: str) -> str:
    """Board-qualified inbox-cursor key (Item 8): ``<board>:<member>``.

    Boards are separate DB files, so genuine cross-board reads stay out of
    scope — this is forward-compat keying only, so a team that later moves
    boards keeps per-board cursors distinct. ``board=None`` maps to the
    :data:`CURSOR_DEFAULT_BOARD` token.
    """

    return f"{board or CURSOR_DEFAULT_BOARD}:{member}"


def _read_cursor(cursors: dict[str, Any], board: Optional[str], member: str) -> int:
    """Read a member's cursor, preferring the board-qualified key and
    falling back to a legacy bare ``<member>`` key (Phase 1 wrote those).
    Returns 0 when neither is present or parseable."""

    raw = cursors.get(_cursor_key(board, member))
    if raw is None:
        raw = cursors.get(member)  # legacy bare-key fallback
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TeamMember:
    """One named teammate in a team's roster."""

    name: str
    profile: str
    role: str
    status: str
    task_id: Optional[str]
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "profile": self.profile,
            "role": self.role,
            "status": self.status,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TeamMember":
        return cls(
            name=d["name"],
            profile=d["profile"],
            role=d.get("role", "teammate"),
            status=d.get("status", "spawning"),
            task_id=d.get("task_id"),
            created_at=float(d.get("created_at", 0.0)),
        )


@dataclass
class TeamRecord:
    """A team's identity + roster, reconstructed from root-task state."""

    id: str
    name: str
    goal: str
    board: Optional[str]
    members: dict[str, TeamMember]
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "board": self.board,
            "members": {n: m.as_dict() for n, m in self.members.items()},
            "created_at": self.created_at,
        }


@dataclass
class TeamMessage:
    """One addressed mailbox message parsed out of a team-root comment.

    ``from_sender`` (not ``from_``) avoids the Python-keyword soft-collision
    and matches the JSON key the tool layer emits (MASTER-PLAN §5, MIN-3).
    """

    id: str
    to: str
    from_sender: str
    body: str
    protocol_type: Optional[str]
    ts: float
    acked: bool


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _require_text(value: Optional[str], field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _require_slug(value: Optional[str], field_name: str) -> str:
    """Validate a team/member name against :data:`SLUG_RE`.

    Raises :class:`ValueError` if empty or containing anything outside
    ``[A-Za-z0-9-]`` / not starting alphanumeric.
    """

    text = _require_text(value, field_name)
    if not SLUG_RE.match(text):
        raise ValueError(
            f"{field_name} {text!r} is not a valid slug; must match "
            r"^[A-Za-z0-9][A-Za-z0-9-]*$ (alphanumerics + hyphen)"
        )
    return text


# ---------------------------------------------------------------------------
# State helpers — JSON-snapshot comments on the team root
# ---------------------------------------------------------------------------

def _write_state(
    conn: sqlite3.Connection, root_id: str, state: dict[str, Any],
) -> int:
    """Append a new state snapshot as a ``[team:state] {...}`` comment.

    Returns the new comment id. State must be JSON-serialisable.
    """

    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return kb.add_comment(
        conn, root_id, author="team-lead", body=STATE_PREFIX + payload,
    )


def _read_state(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    """Return the latest state snapshot, or ``{}`` if none exists.

    Iterates ``list_comments`` in reverse (most recent first) and returns
    the first decodable ``[team:state]`` payload. Older snapshots are
    ignored — last-write-wins, identical to ``kanban_swarm.latest_blackboard``.
    """

    comments = kb.list_comments(conn, root_id)
    for comment in reversed(comments):
        body = comment.body or ""
        if not body.startswith(STATE_PREFIX):
            continue
        try:
            return json.loads(body[len(STATE_PREFIX):])
        except json.JSONDecodeError:
            continue
    return {}


def _emit_team_event(
    conn: sqlite3.Connection, root_id: str, kind: str, payload: dict[str, Any],
) -> None:
    """Append a team-layer event on the team root (Item 7).

    ``kb._append_event`` must run inside an open transaction, so we wrap it in
    ``write_txn``. Emitted on the root so the hook pump's tenant join
    (``tasks.tenant = team:<name>``) picks it up. Used for ``team_idle``, which
    has no native Kanban-event source (idleness is a roster status).
    """

    with kb.write_txn(conn):
        kb._append_event(conn, root_id, kind, payload)


def _update_state(
    conn: sqlite3.Connection, root_id: str, **patch: Any,
) -> dict[str, Any]:
    """Read current state, shallow-merge ``patch``, append a new snapshot.

    Returns the merged dict. Top-level keys in ``patch`` replace top-level
    keys in the snapshot. Callers updating nested structures (e.g.
    ``roster``) pass the fully-rebuilt dict for that key.
    """

    state = _read_state(conn, root_id)
    state.update(patch)
    _write_state(conn, root_id, state)
    return state


# ---------------------------------------------------------------------------
# Team lifecycle — create, lookup, list
# ---------------------------------------------------------------------------

def _record_from_state(team_id: str, state: dict[str, Any]) -> TeamRecord:
    """Rebuild a :class:`TeamRecord` from a state snapshot dict."""

    raw_roster = state.get("roster") or {}
    members = {
        name: TeamMember.from_dict({**m, "name": m.get("name", name)})
        for name, m in raw_roster.items()
    }
    return TeamRecord(
        id=team_id,
        name=str(state.get("name") or ""),
        goal=str(state.get("goal") or ""),
        board=state.get("board"),
        members=members,
        created_at=float(state.get("created_at") or 0.0),
    )


def team_create(
    conn: sqlite3.Connection,
    *,
    name: str,
    goal: str,
    board: Optional[str] = None,
) -> TeamRecord:
    """Create a new team rooted in a sticky-blocked Kanban task.

    The root card is created ``initial_status="blocked"`` directly — the
    kernel honors ``"blocked"`` literally (``kanban_db.create_task``), so it
    parks in ``status='blocked'`` for the rest of its life without the
    create-then-``block_task`` two-step (MASTER-PLAN §2.5, BUG-3). It is
    never auto-dispatched, just a durable parent for state snapshots and
    addressed comments. ``tenant=team:<name>`` tags it for indexed lookup.
    """

    name = _require_slug(name, "name")
    goal = _require_text(goal, "goal")

    root_id = kb.create_task(
        conn,
        title=f"{TEAM_TITLE_PREFIX}{name}]",
        body=(
            f"Team `{name}` root card.\n\n"
            "This card is sticky-blocked and serves as the durable registry "
            "for the team's roster, per-member inbox cursors, mailbox "
            "messages, and idempotency cache. It is never auto-dispatched.\n\n"
            f"Goal:\n{goal}"
        ),
        assignee="team-lead",
        created_by="team-lead",
        board=board,
        tenant=_team_tenant(name),
        initial_status="blocked",
    )

    now = time.time()
    state: dict[str, Any] = {
        "name": name,
        "goal": goal,
        "board": board,
        "created_at": now,
        "roster": {},
        "cursors": {},
        "ops": {},
    }
    # teams-viewer linkage: when a dispatched agent creates a team, record the
    # spawning dispatch task so the viewer can nest the team under it. Env-gated
    # via HERMES_KANBAN_TASK (the dispatcher sets it on every worker — see
    # kanban_db worker spawn); no behavior change when unset.
    parent_dispatch_task = os.environ.get("HERMES_KANBAN_TASK")
    if parent_dispatch_task:
        state["parent_dispatch_task"] = parent_dispatch_task
    _write_state(conn, root_id, state)
    return _record_from_state(root_id, state)


def get_team(conn: sqlite3.Connection, team_id: str) -> TeamRecord:
    """Load a team by its root-task id.

    Raises :class:`KeyError` if the task doesn't exist, :class:`ValueError`
    if the title doesn't carry the ``[team:`` prefix (i.e. it isn't a team
    root).
    """

    task = kb.get_task(conn, team_id)
    if task is None:
        raise KeyError(f"team {team_id!r} not found")
    if not (task.title or "").startswith(TEAM_TITLE_PREFIX):
        raise ValueError(
            f"task {team_id!r} is not a team root (title={task.title!r})"
        )
    state = _read_state(conn, team_id)
    return _record_from_state(team_id, state)


def list_teams(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> list[TeamRecord]:
    """Enumerate every non-archived ``[team:%]`` root task as a TeamRecord.

    We still ``LIKE '[team:%'`` here (not a ``tenant`` scan) because we need
    to enumerate distinct team *roots*, and the root title is the only thing
    that distinguishes a root from its member cards. The literal ``%`` is the
    only wildcard and carries no user input, so it is injection-safe.

    ``board`` is reserved for forward compatibility with the multi-board API
    (Phase 2); the schema has no per-row board column today.
    """

    del board  # reserved for forward compat; see docstring
    rows = conn.execute(
        "SELECT id FROM tasks WHERE title LIKE ? "
        "AND status != 'archived' "
        "ORDER BY created_at ASC",
        (f"{TEAM_TITLE_PREFIX}%",),
    ).fetchall()
    out: list[TeamRecord] = []
    for row in rows:
        try:
            rec = get_team(conn, row["id"])
        except (KeyError, ValueError):
            continue
        # Skip member registry cards ([team:<name>:<member>] role): they share
        # the title prefix but are not team roots. A real root reconstructs a
        # non-empty name with no ':' in it.
        if ":" in rec.name or not rec.name:
            continue
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Member lifecycle — spawn
# ---------------------------------------------------------------------------

def _assert_branch_available(
    conn: sqlite3.Connection, branch_name: Optional[str],
) -> None:
    """Refuse a branch_name already held by a live (non-archived) task.

    Item 6: application-level collision check (no DB UNIQUE constraint).
    ``tasks.branch_name`` is only set on worktree workspaces, so this is a
    forward-looking guard against two teammates colliding on the same branch.
    Archived tasks don't count — their branch can be recycled.
    """

    if not branch_name:
        return
    row = conn.execute(
        "SELECT id FROM tasks WHERE branch_name = ? AND status != 'archived' "
        "LIMIT 1",
        (branch_name,),
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"branch_name {branch_name!r} is already in use by a live task "
            f"({row['id']}); choose a distinct branch for this teammate"
        )


_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def _skills_root() -> Path:
    """Resolve the skills directory a spawned worker would read from.

    Mirrors ``kanban_db._kanban_worker_skill_available``: an unset
    ``HERMES_HOME`` falls back to ``~/.hermes``; skills live under
    ``<home>/skills`` (nested by category).
    """

    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else (Path.home() / ".hermes")
    return base / "skills"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading ``--- ... ---`` YAML frontmatter block from a body."""

    return _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")


def _resolve_agent_skill(agent_type: str) -> tuple[str, str]:
    """Resolve an ``agent_type`` to ``(skill_name, body_without_frontmatter)``.

    Item 10 (thin slice): an ``agent_type`` names a **skill**. We look for a
    ``SKILL.md`` under the worker's skills root whose frontmatter ``name:``
    matches ``agent_type``, falling back to a match on the skill directory
    name. Raises :class:`ValueError` for an unknown ``agent_type`` (no model/
    tools-honoring path — that sub-task is descoped).
    """

    agent_type = _require_text(agent_type, "agent_type")
    root = _skills_root()
    if not root.is_dir():
        raise ValueError(
            f"agent_type {agent_type!r} could not be resolved: no skills "
            f"directory at {root}"
        )

    dir_name_match: Optional[Path] = None
    try:
        for skill_md in root.rglob("SKILL.md"):
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            name_match = _FRONTMATTER_NAME_RE.search(text)
            declared = name_match.group(1).strip() if name_match else None
            if declared == agent_type:
                return agent_type, _strip_frontmatter(text)
            if skill_md.parent.name == agent_type and dir_name_match is None:
                dir_name_match = skill_md
    except OSError:
        pass

    if dir_name_match is not None:
        return agent_type, _strip_frontmatter(
            dir_name_match.read_text(encoding="utf-8")
        )

    raise ValueError(
        f"unknown agent_type {agent_type!r}: no skill with that name was found "
        f"under {root}"
    )


def team_spawn(
    conn: sqlite3.Connection,
    team_id: str,
    member_name: str,
    role: str,
    profile: str,
    prompt: str,
    *,
    skills: Optional[Iterable[str]] = None,
    branch_name: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> TeamMember:
    """Register a named teammate under ``team_id``.

    The member task is a **stand-alone** Kanban task (MASTER-PLAN §2.2,
    BUG-4) — no parent link to the team root, so the kernel lands it in
    ``ready`` on its own (no parents ⇒ ``ready``) and no raw status
    ``UPDATE`` is needed. ``tenant=team:<name>`` groups it; the
    ``[owner:@<member>]`` first-line body header records ownership even when
    profiles collide (§2.3).

    No inline ``dispatch_once`` (§2 ISS-1) and no ``add_notify_sub`` (§2.4,
    BUG-5): the gateway-embedded dispatcher's tick loop claims the ready task
    on its own; the read model is polling-based until Phase 3.

    ``branch_name`` (Item 6, optional) pins the teammate's worktree branch and
    is collision-checked against live tasks before creation; passing it implies
    ``workspace_kind="worktree"`` (``create_task`` rejects a branch on any
    other workspace kind).
    """

    team = get_team(conn, team_id)
    member_name = _require_slug(member_name, "member_name")
    profile = _require_text(profile, "profile")
    prompt = _require_text(prompt, "prompt")
    role = (role or "teammate").strip() or "teammate"

    if member_name in team.members:
        raise ValueError(
            f"team {team.name!r} already has a member named {member_name!r}"
        )

    skills_list = list(skills) if skills else []

    # Item 10 (thin slice): an agent_type names a skill. Append the skill name
    # to the member task's skills list AND append the skill body (frontmatter
    # stripped) to the spawn prompt. Unknown agent_type → ValueError. The
    # model/tools-honoring sub-task is descoped (no model_override path).
    body_prompt = prompt
    if agent_type is not None:
        skill_name, skill_body = _resolve_agent_skill(agent_type)
        if skill_name not in skills_list:
            skills_list.append(skill_name)
        if skill_body.strip():
            body_prompt = f"{prompt}\n\n{skill_body.strip()}"

    skills_arg = skills_list or None

    # Item 6: branch collision safety. Default the workspace to worktree when a
    # branch is supplied (create_task only permits branch_name on worktrees).
    branch_name = (branch_name or "").strip() or None
    if branch_name:
        _assert_branch_available(conn, branch_name)
        if workspace_kind is None:
            workspace_kind = "worktree"
    ws_kind = workspace_kind or "scratch"

    task_id = kb.create_task(
        conn,
        title=f"{TEAM_TITLE_PREFIX}{team.name}:{member_name}] {role}",
        body=f"{_owner_header(member_name)}\n\n{body_prompt}",
        assignee=profile,
        created_by="team-lead",
        tenant=_team_tenant(team.name),
        skills=skills_arg,
        branch_name=branch_name,
        workspace_kind=ws_kind,
    )

    now = time.time()
    member = TeamMember(
        name=member_name,
        profile=profile,
        role=role,
        status="spawning",
        task_id=task_id,
        created_at=now,
    )
    roster = {n: m.as_dict() for n, m in team.members.items()}
    roster[member_name] = member.as_dict()
    _update_state(conn, team_id, roster=roster)
    return member


# ---------------------------------------------------------------------------
# Work task creation — team_task_create
# ---------------------------------------------------------------------------

def _has_plan_approval_response(
    conn: sqlite3.Connection, team_id: str, member: str,
) -> bool:
    """True if a ``plan_approval_response`` involving ``member`` is recorded.

    The handshake is: lead sends ``plan_approval_request`` to the member; the
    member replies ``plan_approval_response``. We treat either direction
    (member as sender, or member as recipient of an approval) as evidence the
    plan was approved for that member. Reads the durable team_messages index.
    """

    row = conn.execute(
        "SELECT 1 FROM team_messages "
        "WHERE team_id = ? AND protocol_type = 'plan_approval_response' "
        "AND (from_sender = ? OR to_member = ?) LIMIT 1",
        (team_id, member, member),
    ).fetchone()
    return row is not None


def assert_member_may_mutate(
    conn: sqlite3.Connection, team_id: str, member: str,
) -> None:
    """Enforce the plan-approval read-only gate for ``member`` (Item 5).

    Allows the mutation when the member is not flagged
    ``requires_plan_approval`` in the roster. When flagged, requires a recorded
    ``plan_approval_response`` involving the member; otherwise raises
    :class:`PlanApprovalRequired`. Scoped to ``team_task_create`` only — the
    mailbox tools (``team_send``/``team_inbox``) stay exempt so the approval
    handshake itself can complete.
    """

    member = _require_slug(member, "member")
    team = get_team(conn, team_id)
    entry = team.members.get(member)
    if entry is None:
        # Unknown members are validated by the caller; nothing to gate here.
        return
    flagged = bool(getattr(entry, "requires_plan_approval", False))
    if not flagged:
        # Roster dataclass may not carry the field; check the raw roster too.
        raw = (_read_state(conn, team_id).get("roster") or {}).get(member) or {}
        flagged = bool(raw.get("requires_plan_approval"))
    if not flagged:
        return
    if _has_plan_approval_response(conn, team_id, member):
        return
    raise PlanApprovalRequired(
        f"member {member!r} requires plan approval before work tasks can be "
        f"created for them; awaiting a plan_approval_response"
    )


def team_task_create(
    conn: sqlite3.Connection,
    team_id: str,
    title: str,
    body: str = "",
    *,
    owner: Optional[str] = None,
    dependencies: Iterable[str] = (),
    branch_name: Optional[str] = None,
    workspace_kind: Optional[str] = None,
) -> str:
    """Create a stand-alone work task scoped to the team.

    No team-root parent link (MASTER-PLAN §2.2). Team membership comes from
    ``tenant=team:<name>``; ownership from the ``[owner:@<member>]`` body
    header. Any caller-supplied ``dependencies`` become real Kanban
    ``task_links`` parents so ``recompute_ready`` gates them — a task with an
    unfinished dependency parks in ``todo`` until the parent is ``done``.

    ``branch_name`` (Item 6, optional) pins a worktree branch and is
    collision-checked against live tasks; passing it implies
    ``workspace_kind="worktree"``.
    """

    title = _require_text(title, "title")
    team = get_team(conn, team_id)

    assignee: Optional[str] = None
    owner_name = OWNER_UNASSIGNED
    if owner is not None:
        owner_name = _require_slug(owner, "owner")
        member = team.members.get(owner_name)
        if member is None:
            raise ValueError(
                f"team {team.name!r} has no member named {owner_name!r}"
            )
        # Item 5: plan-approval gate. Refuse (no task row) when the owner is
        # still gated on approval. Raises PlanApprovalRequired.
        assert_member_may_mutate(conn, team_id, owner_name)
        assignee = member.profile

    full_body = f"{_owner_header(owner_name)}\n\n{body or ''}"
    deps = [d for d in dependencies if d]

    # Item 6: branch collision safety (same forward-looking guard as team_spawn).
    branch_name = (branch_name or "").strip() or None
    if branch_name:
        _assert_branch_available(conn, branch_name)
        if workspace_kind is None:
            workspace_kind = "worktree"
    ws_kind = workspace_kind or "scratch"

    return kb.create_task(
        conn,
        title=title,
        body=full_body,
        assignee=assignee,
        created_by="team-lead",
        tenant=_team_tenant(team.name),
        parents=deps or (),
        branch_name=branch_name,
        workspace_kind=ws_kind,
    )


# ---------------------------------------------------------------------------
# Mailbox — team_send, team_inbox, team_inbox_ack
# ---------------------------------------------------------------------------

def _resolve_worker_member(
    conn: sqlite3.Connection, team_id: str, *, env: Optional[dict] = None,
) -> Optional[str]:
    """Return the roster member name of the dispatcher-spawned worker making
    this call, or ``None`` when the caller is not a worker for this team.

    The trust anchor is :data:`WORKER_TASK_ENV` (``HERMES_KANBAN_TASK``), which
    the dispatcher — not the model — sets to the worker's own member-task id on
    every spawned worker. We map that task id back to its roster member by
    matching ``member.task_id``. This is the team-layer analog of the
    worker-scope gate ``tools/kanban_tools`` already keys on the same env var.

    Returns ``None`` (caller is the lead/session, or a worker whose task is not
    a member of this team) so callers can treat "no provable worker identity"
    as "do not constrain" — the gate this powers (AT-03) only tightens, never
    loosens, behavior. ``env`` is injectable for tests; defaults to
    ``os.environ``.
    """

    environ = os.environ if env is None else env
    worker_task = (environ.get(WORKER_TASK_ENV) or "").strip()
    if not worker_task:
        return None
    try:
        team = get_team(conn, team_id)
    except (KeyError, ValueError):
        return None
    for name, member in team.members.items():
        if member.task_id and str(member.task_id) == worker_task:
            return name
    return None


def _assert_sender_identity(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    from_sender: str,
    protocol_type: Optional[str],
    env: Optional[dict] = None,
) -> None:
    """Reject a spoofed sender on an identity-bound protocol ack (AT-03).

    When ``protocol_type`` is in :data:`_IDENTITY_BOUND_PROTOCOL_TYPES` and the
    caller is a provable dispatcher-spawned worker (see
    :func:`_resolve_worker_member`), require ``from_sender`` to equal that
    worker's own member name; otherwise raise :class:`ValueError`. A no-op for
    non-bound types, and a no-op when no worker identity is provable (the
    lead/session legitimately sends as ``lead`` with no
    :data:`WORKER_TASK_ENV` set) — so the lead path and ordinary chat messages
    are never constrained.
    """

    if protocol_type not in _IDENTITY_BOUND_PROTOCOL_TYPES:
        return
    caller = _resolve_worker_member(conn, team_id, env=env)
    if caller is None:
        return
    if from_sender != caller:
        raise ValueError(
            f"sender {from_sender!r} does not match the calling worker's "
            f"identity {caller!r}; a {protocol_type!r} must be sent as the "
            f"member that owns this worker task (cannot be attributed to "
            f"another member)"
        )


def _format_message_body(
    *, to: str, protocol_type: Optional[str], message: str,
) -> str:
    """Format an addressed-mailbox comment body.

    Shape: ``@<recipient>: [<protocol_type>] <message>``. The sender is *not*
    encoded in the body — it is the comment author (MASTER-PLAN §2 ISS-6).
    The protocol-type bracket is omitted when ``protocol_type`` is None.
    """

    body = f"@{to}: "
    if protocol_type:
        body += f"[{protocol_type}] "
    body += message
    return body


def team_send(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    to: str,
    from_sender: str,
    message: str,
    protocol_type: Optional[str] = None,
) -> str:
    """Append an addressed mailbox message on the team root.

    Returns the new message id (the underlying comment id, stringified). The
    sender is stored as the comment author — there is no forgeable
    ``[from:X]`` body prefix. ``protocol_type`` — when set — must be one of
    :data:`_VALID_PROTOCOL_TYPES`; arbitrary strings are rejected so a typo
    can't silently bypass the recipient's protocol handlers.

    AT-03: for the identity-bound protocol acks
    (:data:`_IDENTITY_BOUND_PROTOCOL_TYPES`), a dispatcher-spawned worker may
    only send as its own roster member — ``from_sender`` is checked against the
    member that owns the worker's task (proven via the trusted
    :data:`WORKER_TASK_ENV`), so a worker cannot forge another member's
    ``shutdown_response`` / ``plan_approval_response``. Raises
    :class:`ValueError` on a mismatch. The lead/session path (no
    :data:`WORKER_TASK_ENV`) is unaffected.

    No ``add_notify_sub`` (MASTER-PLAN §2.4, BUG-5): ``platform="hermes-team"``
    rows are never consumed today, so the read model is polling-based until
    Phase 3 wires real push delivery.
    """

    to = _require_slug(to, "to")
    from_sender = _require_text(from_sender, "from_sender")
    message = _require_text(message, "message")
    if protocol_type is not None:
        protocol_type = str(protocol_type).strip() or None
    if protocol_type is not None and protocol_type not in _VALID_PROTOCOL_TYPES:
        raise ValueError(
            f"protocol_type must be one of {sorted(_VALID_PROTOCOL_TYPES)} "
            f"or None, got {protocol_type!r}"
        )

    # AT-03: an identity-bound protocol ack (shutdown_response /
    # plan_approval_response) cannot be attributed to another member. When the
    # caller is a provable dispatcher-spawned worker, from_sender must be that
    # worker's own member name. No-op for the lead/session path.
    _assert_sender_identity(
        conn, team_id, from_sender=from_sender, protocol_type=protocol_type,
    )

    body = _format_message_body(
        to=to, protocol_type=protocol_type, message=message,
    )
    # CONTRACT (locked by test 1.0): the addressed comment is still written
    # and still fires the `commented` event — Phase 3's notifier polls that
    # signal. The team_messages row below is an *additional* durable index.
    comment_id = kb.add_comment(conn, team_id, author=from_sender, body=body)

    # Item 1: mirror the message into team_messages. If the recipient is in a
    # terminal status (stopped/done) the message can never be delivered, so
    # flag it dead-letter for a recovery verb to requeue deliberately.
    team = get_team(conn, team_id)
    recipient = team.members.get(to)
    dead = bool(recipient and recipient.status in MEMBER_STATUSES_TERMINAL)
    kts.insert_message(
        conn,
        team_id=team_id,
        comment_id=comment_id,
        to_member=to,
        from_sender=from_sender,
        body=message,
        protocol_type=protocol_type,
        board=team.board,
        dead_letter=dead,
    )
    return str(comment_id)


def _parse_message_body(
    body: str, *, recipient: str,
) -> Optional[tuple[str, Optional[str]]]:
    """Parse an addressed-comment body addressed to ``recipient``.

    Returns ``(parsed_body, protocol_type)`` on a hit, or ``None`` if the body
    isn't addressed to this recipient. The sender is *not* parsed here — it
    comes from the comment author.
    """

    addr_tag = f"@{recipient}: "
    if not body.startswith(addr_tag):
        return None
    rest = body[len(addr_tag):]
    protocol_type: Optional[str] = None
    if rest.startswith("["):
        close = rest.find("]")
        if close != -1:
            candidate = rest[1:close]
            if candidate in _VALID_PROTOCOL_TYPES:
                protocol_type = candidate
                rest = rest[close + 1:].lstrip()
    return rest, protocol_type


def team_inbox(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    member: str,
    unread_only: bool = True,
) -> list[TeamMessage]:
    """List addressed messages for ``member`` on this team.

    With ``unread_only=True`` (default), messages with id at or below the
    member's cursor are filtered out. ``unread_only=False`` returns the full
    mailbox history. Cursor advancement happens via :func:`team_inbox_ack`
    only — reading does not auto-advance.
    """

    member = _require_slug(member, "member")
    state = _read_state(conn, team_id)
    cursors = state.get("cursors") or {}
    board = state.get("board")
    # Item 8: cursor key is board-qualified (``<board>:<member>``) with a
    # legacy bare-key fallback for cursors written by Phase 1 code.
    cursor = _read_cursor(cursors, board, member) if unread_only else 0

    out: list[TeamMessage] = []
    delivered_ids: list[int] = []
    # list_comments_after pushes the cursor filter into SQL and id-orders the
    # rows (Item 9), so paging is well-defined even when comments share a
    # created_at second. The comment stream stays the source of truth; the
    # team_messages index is cross-referenced below only to stamp delivery.
    for comment in kts.list_comments_after(conn, team_id, after_id=cursor):
        parsed = _parse_message_body(comment.body or "", recipient=member)
        if parsed is None:
            continue
        parsed_body, protocol_type = parsed
        delivered_ids.append(int(comment.id))
        out.append(TeamMessage(
            id=str(comment.id),
            to=member,
            from_sender=comment.author,
            body=parsed_body,
            protocol_type=protocol_type,
            ts=float(comment.created_at),
            acked=False,
        ))

    # Item 1: stamp delivered_at (once) on the matching team_messages rows.
    kts.stamp_delivered(
        conn, team_id=team_id, to_member=member, comment_ids=delivered_ids,
    )
    return out


def team_inbox_ack(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    member: str,
    through_id: str,
) -> None:
    """Advance ``member``'s inbox cursor strictly monotonically.

    A no-op when ``through_id`` is not greater than the current cursor:
    cursors never move backwards, so an out-of-order ack from a delayed
    worker cannot un-acknowledge later deliveries.
    """

    member = _require_slug(member, "member")
    try:
        new_cursor = int(through_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"through_id must be an int-compatible string, got {through_id!r}"
        ) from exc

    state = _read_state(conn, team_id)
    cursors = dict(state.get("cursors") or {})
    board = state.get("board")
    # Item 8: read with the board-qualified key (legacy bare-key fallback).
    current = _read_cursor(cursors, board, member)

    # Item 1: stamp acked_at (once) on team_messages rows through this id.
    # Done unconditionally — even a re-ack at the same id should leave the
    # acked stamp in place (stamp_acked_through is idempotent on acked_at).
    kts.stamp_acked_through(
        conn, team_id=team_id, to_member=member, through_comment_id=new_cursor,
    )

    if new_cursor <= current:
        return
    # Item 8: always write the board-qualified key, and drop any legacy bare
    # key for this member so the two never drift apart after an upgrade.
    cursors[_cursor_key(board, member)] = new_cursor
    cursors.pop(member, None)
    _update_state(conn, team_id, cursors=cursors)


# ---------------------------------------------------------------------------
# Member updates, status, shutdown, delete
# ---------------------------------------------------------------------------

def team_member_update(
    conn: sqlite3.Connection,
    team_id: str,
    member: str,
    *,
    status: Optional[str] = None,
    **fields: Any,
) -> TeamMember:
    """Patch a member's roster entry. Returns the updated TeamMember.

    ``status`` is promoted to a keyword argument; arbitrary additional fields
    merge via ``**fields``. ``name`` cannot be changed — that would break
    addressed-message routing.
    """

    member_name = _require_slug(member, "member")
    team = get_team(conn, team_id)
    existing = team.members.get(member_name)
    if existing is None:
        raise KeyError(
            f"team {team.name!r} has no member named {member_name!r}"
        )

    if "name" in fields:
        raise ValueError(
            "member name cannot be changed via team_member_update; "
            "addressed-message routing depends on the stable name"
        )

    patch: dict[str, Any] = dict(fields)
    if status is not None:
        patch["status"] = str(status)

    merged = {**existing.as_dict(), **patch, "name": member_name}
    roster = {n: m.as_dict() for n, m in team.members.items()}
    roster[member_name] = merged
    _update_state(conn, team_id, roster=roster)

    # Item 1: a member entering a terminal status can no longer receive mail,
    # so dead-letter any still-undelivered messages addressed to them.
    if status is not None and str(status) in MEMBER_STATUSES_TERMINAL:
        kts.dead_letter_for_stopped(conn, team_id=team_id, member=member_name)

    # Item 7: TeammateIdle has no native Kanban event — emit one when a member
    # transitions to idle so the hook pump can surface it.
    if status is not None and str(status) == "idle":
        _emit_team_event(
            conn, team_id, "team_idle",
            {"member": member_name, "team_id": team_id},
        )

    return TeamMember.from_dict(merged)


def team_status(
    conn: sqlite3.Connection, team_id: str,
) -> dict[str, Any]:
    """Return a status summary for the team: roster + work-task counts.

    Work-task counts come from the indexed ``tenant=team:<name>`` lookup
    (MASTER-PLAN §2.6) — O(log n), no ``task_links`` join (there are no team
    parent links under §2.2) and no body ``LIKE`` scan. Team *cards* (the
    root ``[team:<name>]`` and member registry ``[team:<name>:<member>]``
    cards) share the tenant, so they're excluded by title prefix; what
    remains are the :func:`team_task_create` work tasks.
    """

    team = get_team(conn, team_id)
    card_prefix = f"{TEAM_TITLE_PREFIX}{team.name}"  # [team:<name>  → root + members

    counts: dict[str, int] = {}
    total = 0
    for task in kb.list_tasks(conn, tenant=_team_tenant(team.name)):
        title = task.title or ""
        # Exclude the root ([team:<name>]) and member registry cards
        # ([team:<name>:<member>] role); count only work tasks.
        if title.startswith(card_prefix + "]") or title.startswith(card_prefix + ":"):
            continue
        counts[task.status] = counts.get(task.status, 0) + 1
        total += 1

    task_counts = {
        "total": total,
        "ready": counts.get("ready", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "blocked": counts.get("blocked", 0),
        "todo": counts.get("todo", 0),
        "triage": counts.get("triage", 0),
    }

    return {
        "team_id": team_id,
        "name": team.name,
        "goal": team.goal,
        "members": {
            name: {
                "status": m.status,
                "role": m.role,
                "profile": m.profile,
                "task_id": m.task_id,
            }
            for name, m in team.members.items()
        },
        "tasks": task_counts,
    }


def team_shutdown(
    conn: sqlite3.Connection,
    team_id: str,
    *,
    member: str,
    sender: str = "lead",
    reason: str = "team shutdown",
) -> str:
    """Send a cooperative ``shutdown_request`` to ``member``.

    Thin wrapper over :func:`team_send`. The member's worker is expected to
    reply with ``shutdown_response``; the lead then transitions
    ``team_member_update(status="stopped")`` once the response arrives. Hard
    shutdown via ``worker_pid`` SIGTERM is Phase 3 (MASTER-PLAN §8);
    :func:`team_delete` archives each member task for a coarse hard-shutdown
    in Phase 1.
    """

    return team_send(
        conn, team_id,
        to=member,
        from_sender=sender,
        message=reason,
        protocol_type="shutdown_request",
    )


def team_delete(
    conn: sqlite3.Connection, team_id: str, *, force: bool = False,
) -> None:
    """Archive the team root **and every member task**.

    Refuses while any member is non-terminal (status not in
    :data:`MEMBER_STATUSES_TERMINAL`) unless ``force=True``.

    Archiving each member task triggers Kanban's existing release path: any
    in-flight run is closed as ``reclaimed`` and the task leaves the ready
    queue, so a leftover member task can never be re-claimed by the
    dispatcher after the team is gone (MASTER-PLAN §2 ISS-5 — this is the
    Phase-1 coarse hard-shutdown). The root is archived last.
    """

    team = get_team(conn, team_id)
    active = [
        name for name, m in team.members.items()
        if m.status not in MEMBER_STATUSES_TERMINAL
    ]
    if active and not force:
        raise RuntimeError(
            f"team {team_id!r} has active members: {active}. "
            "Call team_shutdown first, or use force=True."
        )

    # Item 1: the team is going away — dead-letter every member's undelivered
    # mail so it isn't silently lost (a recovery verb can still inspect it).
    for member_name in team.members:
        try:
            kts.dead_letter_for_stopped(conn, team_id=team_id, member=member_name)
        except Exception:
            pass

    for member in team.members.values():
        if not member.task_id:
            continue
        try:
            kb.archive_task(conn, member.task_id)
        except Exception:
            # Best-effort: a member task may already be archived/gone.
            pass

    kb.archive_task(conn, team_id)
