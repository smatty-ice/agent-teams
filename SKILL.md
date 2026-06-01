---
name: agent-teams
description: Use when coordinating Claude-style multi-agent teams in Hermes with a lead, named teammates, shared Kanban task board, comments-as-mailbox, plan approval, quality gates, and cleanup. Backed by the installable agent-teams plugin (17 gated team_* tools).
version: 3.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [multi-agent, orchestration, kanban, delegation, worktrees, tmux, agent-teams]
    related_skills: [hermes-agent, kanban-worker, kanban-orchestrator, subagent-driven-development, multi-agent-debate]
---

# Agent Teams

## Overview

This skill implements a Claude-style "agent teams" workflow in Hermes by composing existing Hermes primitives. It is a playbook, not native `team_*` tooling: Hermes does not yet have first-class teammate registry, live inter-agent chat UI, or built-in team panes. The current session is the team lead. The lead creates a plan, assigns named teammates, gives them independent work, coordinates through a shared board, waits for their results, enforces quality gates, synthesizes the final answer, and cleans up the team.

This is not the same as simply spawning a few subagents. In a team, work is coordinated through durable shared state:

- Lead: the current Hermes session; owns planning, coordination, approvals, integration, and final synthesis.
- Teammates: `delegate_task` subagents, dispatcher-spawned Kanban workers, `hermes chat -q` processes, or tmux panes.
- Shared task list: a Hermes Kanban board for durable work, or the local `todo` tool only for small in-session teams.
- Mailbox: Kanban comments and task logs, using explicit prefixes such as `STATUS:`, `BLOCKED:`, `HANDOFF:`, and `DONE:`.
- Quality gates: plan approval, spec review, code review, tests, integration review, and final lead synthesis.

Prefer this skill when teammates need separate context windows, independent investigation, adversarial review, or durable task coordination. Prefer a normal `delegate_task` when a helper only needs to return one focused result to the caller.

## When to Use

Use agent teams for:

- Research or review where multiple perspectives add value.
- Debugging with competing hypotheses that should be tested in parallel.
- Features split across independent files or layers, such as backend, frontend, tests, and docs.
- Code review from separate lenses: security, performance, test coverage, maintainability.
- Architecture debates that need explicit adversarial positions and a judge.
- Durable multi-step work that may survive context compression, restart, or worker failures.
- Work where task ownership, handoff notes, logs, and artifacts matter.

Do not use agent teams for:

- A small single-file edit.
- A sequential task where every step depends on the previous result.
- Work where multiple teammates would edit the same files at the same time.
- Tasks involving secrets, credential stores, payment flows, or production data without explicit user direction and strict gating.
- Work without clear acceptance criteria or a test/review path.
- Cases where spawning more agents would be slower than doing the task directly.

## Map Claude-Style Teams to Hermes Primitives

| Agent team concept | Hermes implementation |
| --- | --- |
| Team lead | Current Hermes session |
| Teammate | `delegate_task`, Kanban worker, `hermes chat -q`, tmux pane, or optional external agent lane |
| Shared task list | `hermes kanban` board for durable work; `todo` for tiny local teams |
| Task claiming | Kanban dispatcher, assigned tasks, task status, links/dependencies |
| Mailbox | Kanban comments and logs with message prefixes |
| Direct teammate messages | Comments addressed to `@role`, `@profile`, or a task id |
| Display mode | In-process delegation, background processes, tmux panes, dashboard/watch commands |
| Plan approval | Lead presents team plan before implementation |
| Teammate model/role | Hermes profile, `--model`, `--provider`, `--skills`, and prompt role |
| Isolated implementation | `--worktree`, Kanban `--workspace worktree`, or separate checkout |
| Quality hooks | Tests, linters, spec reviewer, quality reviewer, integration reviewer |
| Shutdown | Stop tmux/background sessions, archive/complete tasks, remove temporary worktrees |

## Operating Modes

### 1. Lightweight in-process mode

Use this when the work is bounded and the lead can wait for all teammates to return:

- Spawn multiple `delegate_task` workers.
- Give each worker a distinct role and non-overlapping scope.
- Use the `multi-agent-debate` pattern for decisions with legitimate competing options.
- Use the `subagent-driven-development` pattern for implementation tasks: implementer, spec reviewer, quality reviewer, then final integration review.

This mode is best for research, review, debate, and short implementation spikes. It is not durable; if the parent session is interrupted, child work is cancelled.

### 2. Durable Kanban mode

Use this for real agent-team work. Kanban gives the team durable state, task dependencies, logs, comments, worker dispatch, retries, and board-level supervision.

Common setup:

```bash
hermes kanban init
hermes gateway start
hermes profile list
```

Create role profiles when useful. Profiles are persistent and `--clone` copies the active profile's config and `.env`; only clone into trusted local profiles with explicit user approval. Prefer existing profiles when they already exist, and clean up throwaway profiles after the team is done.

```bash
hermes profile create lead --clone --description "Lead/orchestrator: decomposes goals, creates Kanban tasks, links dependencies, monitors results, and synthesizes."
hermes profile create researcher --clone --description "Researcher: gathers evidence, cites sources, and writes concise findings."
hermes profile create implementer --clone --description "Implementer: edits code in isolated workspaces/worktrees and runs tests."
hermes profile create reviewer --clone --description "Reviewer: checks diffs, tests, edge cases, security, and asks for fixes."
hermes profile create writer --clone --description "Writer: synthesizes final reports and user-facing documentation."
```

Create a root task, record its id, then create linked children:

```bash
hermes kanban create "Ship API rate limiting" \
  --assignee lead \
  --workspace dir:/path/to/repo \
  --priority 10

hermes kanban create "Research API rate limiting strategy" \
  --assignee researcher \
  --workspace dir:/path/to/repo \
  --parent <root-task-id> \
  --priority 9

hermes kanban create "Implement API rate limiting" \
  --assignee implementer \
  --workspace worktree \
  --branch wt/api-rate-limiting \
  --parent <research-task-id> \
  --skill software-development \
  --priority 8

hermes kanban create "Review API rate limiting implementation" \
  --assignee reviewer \
  --workspace dir:/path/to/repo \
  --parent <implementation-task-id> \
  --skill github-code-review \
  --priority 7
```

Use the board to watch and steer:

```bash
hermes kanban watch
hermes kanban stats
hermes kanban list --status blocked
hermes kanban show <task-id>
hermes kanban log <task-id>
hermes kanban runs <task-id>
hermes kanban comment <task-id> "HANDOFF: Use Redis-backed limiting; memory-only is not acceptable."
hermes kanban unblock <task-id>
```

Prefer dispatcher-spawned Kanban workers for normal durable work. They receive task-scoped Kanban tools and environment such as `HERMES_KANBAN_TASK` and `HERMES_KANBAN_BOARD`.

### 3. tmux split-pane mode

Use this when the user wants visible concurrent teammates or when an interactive worker needs direct steering.

Example skeleton:

```bash
tmux new-session -d -s agent-team

tmux rename-window -t agent-team:0 board
tmux send-keys -t agent-team:board 'hermes kanban watch' C-m

tmux new-window -t agent-team -n lead
tmux send-keys -t agent-team:lead 'hermes -p lead chat --toolsets kanban -s agent-teams' C-m

tmux new-window -t agent-team -n researcher
tmux send-keys -t agent-team:researcher 'HERMES_KANBAN_TASK=<task-id> hermes -p researcher chat --skills kanban-worker -q "Work your assigned task. First inspect the task, post STATUS comments, then complete or block with evidence."' C-m

tmux new-window -t agent-team -n implementer
tmux send-keys -t agent-team:implementer 'HERMES_KANBAN_TASK=<task-id> hermes -p implementer chat --worktree --skills kanban-worker -q "Work your assigned task in an isolated workspace. Run tests and report changed files."' C-m

tmux attach -t agent-team
```

Use tmux for supervision and demos. For unattended execution, use the Kanban dispatcher instead of hand-managed panes.

### 4. Optional external-agent lane

External coding agents such as Blackbox, Codex, Claude Code, or OpenCode can be used as specialist teammates, but Hermes remains the lead and final reviewer.

For Blackbox specifically:

```bash
command -v blackbox
blackbox --version
```

If Blackbox is missing or not configured, skip the Blackbox lane. Do not block the team unless the user explicitly required Blackbox output. If it is available, inspect `blackbox --help` because CLI flags can change, then run it only in an isolated worktree and review its diff before accepting anything. Example shape:

```bash
blackbox --prompt "Implement <task> in this worktree. Keep scope bounded. Run tests and summarize changed files."
```

Blackbox output is an untrusted proposal until Hermes runs its own spec review, quality review, and tests.

## Team Startup Protocol

Before spawning teammates, the lead must do the setup work.

1. Understand the goal, repo/workspace, constraints, and risk level.
2. Decide whether a team is justified; if not, use direct work or one `delegate_task`.
3. Choose operating mode: lightweight, durable Kanban, tmux, or hybrid.
4. Check prerequisites: `hermes`, `tmux` if requested, Kanban if durable, optional external agents only if requested.
5. Draft a team plan.
6. Get user approval before implementation unless the user explicitly said to proceed autonomously.
7. Create the board/tasks or local todo list.
8. Spawn teammates with complete context.
9. Monitor progress and blockers.
10. Enforce review/test gates.
11. Synthesize final result.
12. Shut down and clean up.

## Plan-Approval Gate

For complex, risky, or code-modifying teams, produce a plan before spawning implementers. The plan must include:

- Goal and success criteria.
- Scope and non-goals.
- Chosen operating mode.
- Teammate roster: role, profile/model if known, responsibilities, and stop condition.
- Task graph with dependencies.
- Workspace strategy: shared checkout, worktree, scratch, or separate repo clone.
- Likely file/module ownership and conflict risk.
- Communication protocol and board/comment conventions.
- Quality gates: tests, reviewers, integration checks, and final acceptance.
- Cleanup plan.

If the user rejects the plan, revise it. If the user says "go ahead" or has already given explicit autonomy, proceed and record the assumptions.

## Role Patterns

Common teammate roles:

- Architect: proposes decomposition, APIs, boundaries, and risks.
- Researcher: gathers sources, docs, examples, benchmarks, and constraints.
- Implementer: owns a bounded code change in an isolated workspace.
- Test engineer: writes or expands tests and runs verification.
- Reviewer: checks spec compliance, quality, edge cases, and security.
- Debugger: investigates one root-cause hypothesis and tries to falsify it.
- Devil's advocate: argues against the apparent best plan.
- Integrator: merges or reconciles work after review.
- Writer: produces final docs, release notes, or handoff summary.

When a decision has multiple viable paths, use `multi-agent-debate`: assign positions, then spawn evaluators to rank the options. When executing a plan, use `subagent-driven-development`: implementer, spec reviewer, quality reviewer, then final integration review.

## Teammate Prompt Template

Use this template when spawning any teammate, whether via `delegate_task`, Kanban, or a `hermes chat -q` process.

```text
You are a teammate in a Hermes agent team.

Team goal: <goal>
Role: <role>
Board/task: <board and task id, if any>
Workspace: <path or worktree>
Assigned scope: <specific files/modules/questions>
Acceptance criteria: <clear checklist>
Stop condition: <when to finish or block>

Rules:
- Work only on your assigned scope unless the lead reassigns you.
- Do not overwrite another teammate's work.
- Before editing shared files, check for conflicts and ownership.
- Post durable updates using the team's comment protocol.
- If blocked, explain exactly what is needed and mark the task blocked.
- Run the required checks yourself; do not rely on another agent's self-report.
- On completion, report summary, artifacts, changed files, tests/checks run, residual risks, and follow-up tasks.
- Do not declare DONE unless the acceptance criteria pass.
```

## Mailbox and Comment Protocol

All durable team communication goes on the board. Use these prefixes so the lead can scan quickly:

- `STATUS:` progress update.
- `QUESTION:` needs lead or user input.
- `BLOCKED:` cannot proceed; include the exact missing input or failing command.
- `HANDOFF:` information intended for another teammate.
- `REVIEW:` review findings or requested changes.
- `RISK:` concern that might affect integration, safety, or timeline.
- `DONE:` completion summary with artifacts and verification.

Important direct messages must be copied to the relevant task. The lead owns synthesis and conflict resolution.

## Worktree and File-Conflict Discipline

Parallel implementation is dangerous without ownership boundaries.

Rules:

1. Start from a clean git status before spawning implementation teammates.
2. Assign one owner per file or module.
3. Use Kanban `--workspace worktree` or `hermes --worktree` for independent code changes.
4. Do not let two teammates edit the same file simultaneously. If unavoidable, serialize the tasks.
5. Have implementers report changed files and tests run.
6. Have reviewers inspect actual diffs, not just completion summaries.
7. The lead integrates only after spec, quality, and tests pass.

Useful checks:

```bash
git status --short --branch
git diff --stat
git diff --check
git worktree list
```

## Quality Gates

Minimum gates for code-modifying teams:

1. Pre-flight gate: plan approved, repo state inspected, workspaces chosen.
2. Implementation gate: each implementer completes only its assigned acceptance criteria.
3. Spec review gate: a reviewer checks the result against the original task.
4. Quality review gate: a reviewer checks style, maintainability, tests, security, and edge cases.
5. Verification gate: lead or reviewer runs canonical tests/linters.
6. Integration gate: lead checks that outputs from different teammates fit together.
7. Final synthesis gate: lead reports done work, evidence, artifacts, risks, and next actions.

Never accept "agent says it passed" as proof. Verify with tools, files, tests, logs, or a second reviewer.

## Monitoring Commands

For Kanban teams:

```bash
hermes kanban list
hermes kanban watch
hermes kanban stats
hermes kanban show <task-id>
hermes kanban log <task-id>
hermes kanban runs <task-id>
hermes kanban tail <task-id>
```

For terminal background workers:

- Use `process(action="poll")` for progress.
- Use `process(action="log")` for output.
- Use `process(action="wait")` for bounded completion.
- Use `process(action="kill")` only for stuck or unsafe workers.

For tmux teams:

```bash
tmux ls
tmux capture-pane -t agent-team:<window> -p -S -80
tmux send-keys -t agent-team:<window> '<message>' C-m
tmux kill-session -t agent-team
```

## Shutdown and Cleanup

A team is not done until the lead has closed the loop.

Checklist:

- [ ] Collect final summaries from all teammates.
- [ ] Resolve or document blocked tasks.
- [ ] Run final tests/reviews or explain why they were not run.
- [ ] Inspect git status/diff and worktree state.
- [ ] Stop unneeded background processes and tmux sessions.
- [ ] Remove temporary worktrees only after their changes are accepted, copied, or intentionally discarded.
- [ ] Mark tasks complete, blocked, or archived.
- [ ] Produce a final synthesis for the user with artifacts, verification, risks, and next action.

Commands:

```bash
tmux ls
tmux kill-session -t agent-team
hermes kanban list --status blocked
hermes kanban archive <task-id>
git worktree list
git status --short --branch
```

## Example Requests

Parallel code review:

```text
Use agent-teams to review PR #142. Create security, performance, and test-coverage reviewers. Have each report findings, then synthesize a ranked list of issues.
```

Competing debugging hypotheses:

```text
Use agent-teams to investigate why the app exits after one message. Spawn five debugger teammates with different hypotheses. Have them try to falsify each other's theories and converge on a consensus.
```

Cross-layer feature:

```text
Use agent-teams to add rate limiting. Research the approach, implement backend changes in a worktree, add tests, review the diff, then write a final handoff.
```

Architecture debate:

```text
Use agent-teams and multi-agent-debate to compare a skill-only implementation, a plugin-backed implementation, and a core-tool implementation. Have judges rank the options by usability, safety, and implementation cost.
```

## Common Pitfalls

1. Treating teams as just parallel subagents. Real teams need a shared board, ownership, handoffs, and synthesis.
2. Skipping plan approval on risky work. This causes mis-scoped teammates and wasted edits.
3. Spawning too many teammates. Start with 3-5 roles; add more only if the task genuinely benefits.
4. Letting teammates edit the same files. Assign ownership or serialize tasks.
5. Using scratch workspaces for outputs that must persist. Use worktrees or durable directories.
6. Relying on ephemeral chat for durable decisions. Put decisions and handoffs on the board.
7. Accepting self-reported success. Run tests, inspect files, or dispatch reviewers.
8. Forgetting cleanup. Orphaned tmux sessions, background processes, and worktrees create confusion.
9. Assuming Blackbox or another external agent is installed. Check first and degrade gracefully.
10. Making the lead do substantial parallel implementation work that prevents coordination and review. The lead should coordinate, unblock, review, integrate small fixes when appropriate, and synthesize.
11. Creating vague cards. Tasks need acceptance criteria, ownership, and stop conditions.
12. Over-parallelizing hidden dependencies. If tasks depend on each other, link them and let blocked tasks wait.

## Verification Checklist

Before final response, verify:

- [ ] The selected operating mode fits the task size and risk.
- [ ] The plan was approved or explicit autonomy was given.
- [ ] Teammate roles and task ownership were clear.
- [ ] Shared files were conflict-checked.
- [ ] Durable comments/logs captured important handoffs.
- [ ] Required tests, reviews, or checks were run.
- [ ] Background sessions or tmux panes were cleaned up.
- [ ] The final synthesis includes what changed, who/which role did it, artifacts, verification, risks, and next steps.

## Native Team Tools (shipped as the `agent-teams` plugin)

The patterns above are still the operator playbook, but the `agent-teams` **plugin** also exposes a Claude-parity tool surface. The plugin registers **17** gated `team_*` tools (9 Phase-1 + 8 Phase-2) via its `register(ctx)` entrypoint (`plugins/agent-teams/__init__.py` → `ctx.register_tool(...)`); the domain logic lives in `plugins/agent-teams/kanban_team.py` and the tool schemas/handlers/gating in `plugins/agent-teams/team_tools.py`. Nothing is added to Hermes core — the plugin depends only on stable vanilla-Hermes `hermes_cli.kanban_db` APIs and creates its own tables idempotently (see Storage model).

### Install and enable

```bash
# Install from a Git repo you control (or copy the dir into ~/.hermes/plugins/):
hermes plugins install <owner>/<repo>
#   or:  cp -r plugins/agent-teams ~/.hermes/plugins/agent-teams
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [agent-teams]
```

Then gate the tools on (either is sufficient — see Enabling below):

- `export HERMES_TEAM_LEAD=1`, OR
- add `team` to the profile's `toolsets:` list.

Start a fresh session and confirm `/toolsets` shows `team [17]`. The plugin lives in `~/.hermes/plugins/`, which is **outside** the `~/.hermes/hermes-agent/` checkout that Hermes `git reset --hard`s on update, so it survives Hermes auto-updates. See `plugins/agent-teams/README.md` for verification and the optional `hermes team-watch` daemon.

### Phase 1 tools (9)

The nine Phase-1 tools:

| Tool | Purpose |
|---|---|
| `team_create` | Create a team rooted in a sticky-blocked Kanban task. |
| `team_spawn` | Register a named teammate and dispatch their initial task. |
| `team_task_create` | Create a work task scoped to the team (resolves `owner` → member's profile). |
| `team_send` | Send a DM to a teammate by stable name. Supports `protocol_type` (`shutdown_request`, `shutdown_response`, `plan_approval_request`, `plan_approval_response`). |
| `team_inbox` | Read messages addressed to a member; `unread_only=true` (default) filters by per-member cursor. |
| `team_inbox_ack` | Advance the member's cursor strictly monotonically. |
| `team_status` | Snapshot of roster + work-task counts. |
| `team_shutdown` | Cooperative `shutdown_request` to a member. |
| `team_delete` | Archive the team root and each member task; refuses while members are non-terminal unless `force=true`. |

### Native Team Tools (Phase 2 — shipped)

Phase 2 adds six more gated tools (same `team` toolset / `HERMES_TEAM_LEAD` gate):

| Tool | Purpose |
|---|---|
| `team_debug_bundle` | Read-only dump of a team's full durable state (team record, state snapshot, `team_operations` journal, `team_messages`, tasks, links, comments, events, runs, profiles, config, recent failure logs, replay instructions). |
| `team_export_config` | Return a Claude-shaped `{name, goal, board, members:[…]}` config JSON (the parity analog of `~/.claude/teams/<name>/config.json`); returns the JSON, does not write a file. |
| `team_inspect` | Read-only deep view of one task, member, or journaled operation. |
| `team_replay` | Reset a `failed`/`refused` journaled operation back to `pending` so the next identical tool call re-attempts it idempotently. |
| `team_requeue` | Clear `dead_letter` and reset `delivered_at` on a `team_messages` row so it is redelivered on the recipient's next inbox read. |
| `team_mark_blocked` | Lead-initiated block of a team task with a reason (wraps Kanban `block_task`). |
| `team_restore_assignment` | Re-attach the `[owner:@<member>]` body header + assignee to an orphaned task. |
| `team_explain_blockage` | Read-only: report a task's unmet (non-`done`) dependency parents + last failure. |

`team_spawn` also gains two optional parameters in Phase 2:

- `agent_type` (thin-slice subagent-definition reuse) — names a **skill**; its name is appended to the teammate task's `skills` and its `SKILL.md` body (frontmatter stripped) is appended to the spawn prompt. Unknown names error. Honoring a skill/profile's `model`/`tools` is intentionally **descoped** (no `model_override` path on `create_task`).
- `branch_name` — pins a worktree branch, collision-checked against live tasks before creation (implies `workspace_kind="worktree"`).

#### Descoped recovery verbs (documented, not stubbed)

The recovery surface ships the curated **6** above. Three verbs from the original list are **descoped** because Hermes has no backing primitive for them; do not expect a tool:

- `revoke_agent` — fold into `team_shutdown` + `team_delete(force=true)` (which archives the member task and triggers the existing reclaim path).
- `freeze_lane` — no kernel primitive for freezing a board lane.
- `escalate_to_orchestrator` — there is no orchestrator endpoint to escalate to.

### Enabling

The plugin must be enabled (`plugins.enabled: [agent-teams]` in `~/.hermes/config.yaml`) **and** the tools are individually gated. To make the `team_*` tools visible, satisfy the gate (`_profile_has_team_toolset` in `plugins/agent-teams/team_tools.py`):

- **Env**: `export HERMES_TEAM_LEAD=1` (also accepts `true`, `yes`), OR
- **Profile**: add `team` to the `toolsets:` list in your profile config.

The gate is passed to `ctx.register_tool(..., check_fn=...)`, so a tool stays hidden until one of the two conditions holds — this mirrors the list-shape kanban-toolset gating in vanilla core.

### Storage model

Team state — roster, per-member inbox cursors — lives as JSON-prefixed comments on the team's root Kanban task (prefix `[team:state] {...}`), mirroring the `[swarm:blackboard]` pattern in `hermes_cli/kanban_swarm.py:26`. Mailbox messages are addressed Kanban comments on the same root: `@<recipient>: [<protocol_type>] <body>`, where the sender is the comment **author** (no forgeable `[from:X]` body prefix).

**Plugin-owned storage** adds three SQLite tables. The plugin **owns** their DDL — there is **no core schema dependency**. They are created idempotently (`CREATE TABLE IF NOT EXISTS`) on every connect by `ensure_tables(conn)` in `plugins/agent-teams/kanban_team_store.py`, which the tool path (`team_tools._conn()`) and the watcher both call after `kb.connect()`. So the plugin runs against a vanilla Hermes whose core never had these tables (proven by `tests/verify_install.py`, which drops them and runs the full lifecycle through the plugin handlers):

- `team_operations` — durable cross-tool idempotency journal `(operation_id, team_id, verb, input_hash, status, result_json, error)`. Promotes the Phase-1 `state["ops"]` cache; survives restart and now lets `team_create` dedup too. `operation_id` is derived from the input hash (no `operation_id` tool parameter).
- `team_messages` — a durable mailbox **index** paralleling the addressed comment, carrying `delivered_at` vs `acked_at` and a `dead_letter` flag. The addressed comment (and its `commented` event) remain the source-of-truth signal; `team_messages` is an additional index, never a replacement. `team_send` dual-writes both; `team_inbox` stamps `delivered_at` (once); `team_inbox_ack` stamps `acked_at`; sends to a terminal-status member and `team_delete` dead-letter undelivered mail (recover with `team_requeue`).
- `team_hook_subs` — a registry over `task_events` for the `TaskCreated`/`TaskCompleted`/`TeammateIdle` hooks (`plugins/agent-teams/kanban_team_hooks.py`: `register_hook`/`poll_hook`/`ack_hook`). `TeammateIdle` has no native Kanban event, so `team_member_update(status="idle")` emits a synthetic `team_idle` event on the team root; the pump filters events by hook kind AND the team's tenant.

**Inbox cursor keys** are board-qualified (`<board>:<member>`, Phase 2 Item 8) with a legacy bare-`<member>` fallback. Boards are separate DB files, so genuine cross-board reads stay out of scope — this is forward-compat keying only.

**Plan-approval enforcement** (Phase 2 Item 5): a member flagged `requires_plan_approval=true` (via `team_member_update`) cannot have a work task created for them (`team_task_create owner=…`) until a `plan_approval_response` involving them is recorded; the tool refuses with a journaled `status="refused"` op and creates no task. `team_send`/`team_inbox` stay exempt so the approval handshake can complete.

Member and work tasks are **stand-alone** Kanban tasks (no parent link to the team root). Team membership is recorded with `tenant=team:<name>` on every team task — the indexed `tasks(tenant)` column gives `team_status`/`team_delete` O(log n) enumeration. Per-task ownership is a first-line body header `[owner:@<member>]` (sentinel `[owner:@-]` for unassigned), so two members sharing a profile stay distinguishable. The team root is created `initial_status="blocked"` directly (sticky-blocked registry, never auto-dispatched); member tasks have no parents so the kernel parks them in `ready` for the gateway dispatcher to claim. `team_*` tools never write `add_notify_sub` rows (the read model is polling-based until Phase 3).

### Idempotency

Mutating handlers hash their input args (sha256[:16] over `(verb, sorted args)`) and journal the result in `team_operations` (Phase 2). A duplicate call with identical args returns the journaled prior result instead of double-writing, and the dedup survives restart. As of Phase 2 `team_create` is dedupable too (the journal's `team_id` is nullable, so the not-yet-created team is no obstacle). A legacy `state["ops"]` cache is still read as a fallback for teams created on the Phase-1 code path.

## Native Team Runtime (Phase 3 — shipped)

Phase 3 adds three *active* behaviors on top of the Phase 1 substrate. The pure domain code lives in `plugins/agent-teams/kanban_team_runtime.py` (no gateway import); the watcher that drives it lives in `plugins/agent-teams/watcher.py` (re-homed onto plugin-safe surfaces — see Watcher below). **No new SQLite tables and no new tool names** — Phase 3 reuses the existing `[team:state]` JSON snapshot for its cursors. The only edit to the Phase 1 surface is one new optional parameter on `team_shutdown`.

### Item 1 — per-member hard shutdown with grace window

`team_shutdown(team_id, member, timeout_seconds=N)` now escalates. After sending the cooperative `shutdown_request`, the handler calls `kanban_team_runtime.team_member_hard_shutdown`, which:

1. Polls the **lead's** inbox for a `shutdown_response` from the member during an `N`-second grace window. On a hit → mark the member `stopped`, return `outcome="cooperative"` (the worker is never touched).
2. On timeout → `kanban_db.reclaim_task(...)` (SIGTERM → wait → SIGKILL via `_terminate_reclaimed_worker`, host-locality gated) **and then `kanban_db.archive_task(...)`**. The archive is load-bearing: `reclaim_task` re-readies the task, so without the archive the dispatcher would simply re-spawn the member. The member ends `stopped` and its task ends `archived`.

The clock (`now_fn`/`sleep_fn`), kill fn (`signal_fn`), are injectable so the behavior is unit-tested with no real subprocess or signal. Omitting `timeout_seconds` (or `0`) keeps the original cooperative-only behavior.

### Item 2 — auto-idle on dispatcher run completion

`run_auto_idle_pass(conn)` scans each team's member tasks for new run-terminal `task_events` (`completed`/`blocked`/`gave_up`/`crashed`/`timed_out`/`reclaimed`/`archived`) past a per-team `auto_idle_cursor` and auto-transitions the roster member — `idle` by default, `stopped` on `archived`. Members already in a terminal roster status (`stopped`/`done`) are skipped so a deliberate stop is never reverted. The globally-monotonic `task_events.id` cursor makes the pass idempotent across runs.

### Item 3 — true push-based message delivery

On the one-shot-worker substrate, "push" = re-readying the recipient's task so the dispatcher re-spawns it to process its inbox. `run_push_delivery_pass(conn)` finds new addressed comments past a per-team `push_cursor` and calls `wake_member`, a guarded re-ready `UPDATE` (`status NOT IN ('running','archived','done')`) so a live worker or a deliberately-stopped task is never resurrected. `@lead:` recipients are skipped (the lead is a session, not a member task).

### Watcher (pure-plugin: throttled hook + optional daemon)

The auto-idle + push passes are re-homed onto plugin-safe surfaces (`plugins/agent-teams/watcher.py`) because there is **no plugin hook for a free-running gateway coroutine**. Both surfaces delegate to `_tick_all_boards()`, which enumerates boards via `kb.list_boards()`, calls `ensure_tables`, skips boards with no teams, then runs `run_auto_idle_pass` followed by `run_push_delivery_pass`:

- **Throttled hook** — `register(ctx)` wires `run_passes_throttled` onto the `pre_gateway_dispatch` hook. It fires on gateway traffic, at most once per `WATCH_INTERVAL` (5s), and is **observer-only**: it always returns `None` (never influences dispatch) and swallows exceptions so a misbehaving pass can never disrupt the gateway. This covers active team-lead use, but **will NOT fire on a fully idle gateway** (no traffic → no hook).
- **Optional daemon** — `hermes team-watch` runs `watch_daemon`, a blocking loop that ticks every 5s regardless of gateway traffic, giving true timer-based idle delivery. It is registered via `ctx.register_cli_command("team-watch", ...)`. Run it in its own terminal/`tmux` pane when you need guaranteed idle-time delivery.

The 17 tools + tables work fully without the watcher (manual `team_member_update` + `team_inbox` polling = the Phase-1 model), so the feature is complete even if neither watcher surface is active.

### Known non-parity with Claude Code teams

- `team_send` durably records every message: the Phase-2 `team_messages` table tracks `delivered_at`/`acked_at`/`dead_letter`, and **Phase 3 adds true push delivery** — the watcher re-readies a recipient's task when a new addressed comment arrives, so the lead no longer has to poll on a teammate's behalf. As a plugin, push runs throttled on the `pre_gateway_dispatch` hook (active use) or on the opt-in `hermes team-watch` daemon (idle gateways); polling (`team_inbox`) still works as a fallback in all cases.
- Hard shutdown (SIGTERM via `worker_pid`) is **shipped in Phase 3**: `team_shutdown(..., timeout_seconds=N)` waits for a cooperative `shutdown_response`, then escalates to `reclaim_task` (SIGTERM→SIGKILL via `_terminate_reclaimed_worker`) **and** `archive_task` so the member can't be re-spawned. `team_delete` still archives each member task for a coarse hard-shutdown.
- Plan-approval enforcement at the tool layer (refusing `team_task_create` for a member flagged `requires_plan_approval` until a `plan_approval_response` is recorded) is **shipped in Phase 2**; the refusal is journaled in `team_operations`.
- The `TaskCreated`/`TaskCompleted`/`TeammateIdle` hooks ship in Phase 2 as a poll-based registry (`team_hook_subs` + `kanban_team_hooks.poll_hook`), not automatic push callbacks — a consumer polls. **Phase 3 adds auto-idle-on-run-completion** (`run_auto_idle_pass`, driven by the watcher) so `TeammateIdle` fires without a manual `team_member_update`.
- **Watcher coverage tradeoff** (plugin reality): auto-idle + push run throttled on the `pre_gateway_dispatch` hook by default, which covers active use but does **not** fire on a fully idle gateway. For guaranteed idle-time delivery, run the opt-in `hermes team-watch` daemon (true 5s timer). There is no plugin hook for a free background gateway coroutine, so this two-surface model replaces the always-on gateway coroutine the core prototype used.
- Subagent-definition reuse (`team_spawn agent_type`) is the **thin slice**: a skill's name + body are merged into the teammate. Honoring a skill/profile's `model`/`tools` (a `model_override` path on `create_task`) is descoped.
- The recovery surface ships the curated **6** verbs (see above); `revoke_agent`, `freeze_lane`, and `escalate_to_orchestrator` are descoped (no backing primitive).
- Multi-board teams: cursor keys are board-qualified for forward compatibility, but a single team still operates on one board DB (genuine cross-board reads are out of scope — boards are separate DB files).
- No tmux split-pane UI — see Operating Mode 3 above for the operator-driven equivalent.
- One team per lead, no nested teams, no lead transfer (matches Claude's own current limitations).

### References

All paths are inside the plugin (`plugins/agent-teams/`):

- `__init__.py` — plugin entrypoint: `register(ctx)` registers the 17 tools, the `pre_gateway_dispatch` watcher hook, and the `team-watch` CLI command.
- `kanban_team.py` — domain logic (dataclasses, state helpers, team_* functions, plan-approval gate, branch hook, `agent_type` resolver).
- `team_tools.py` — tool `SCHEMAS`/`HANDLERS`/`EMOJIS`, the `_profile_has_team_toolset` gate, journal-backed idempotency.
- `kanban_team_store.py` — `ensure_tables` (idempotent DDL for the 3 tables) + `team_operations` journal / `team_messages` index helpers.
- `kanban_team_hooks.py` — hook registry + pump over `task_events`.
- `kanban_team_recovery.py` — debug bundle / export config + the 6 recovery verbs.
- `kanban_team_runtime.py` — runtime passes (hard shutdown, `run_auto_idle_pass`, `run_push_delivery_pass`).
- `watcher.py` — the plugin watcher: `run_passes_throttled` (`pre_gateway_dispatch`, throttled, observer-only) + `watch_daemon` (`hermes team-watch`).
- `plugin.yaml` — plugin manifest (name, version, `provides_tools`, `provides_hooks`).
- `README.md` — install / enable / verify, the optional daemon, and the durability win.
- `tests/` — full unit/gating/lifecycle/runtime coverage (`test_kanban_team*.py`, `test_team_tools.py`, `test_watcher.py`, `test_team_watcher_tick.py`, `test_plugin_load.py`, `test_store_rehome.py`) plus the runnable real-loader proof `tests/verify_install.py`.
- `../../docs/plans-eval/PLUGIN-REPACKAGE-PLAN.md` (plus `MASTER-PLAN.md` / `PHASE-2-PLAN.md` / `PHASE-3-PLAN.md`) — the implementation plans.

### Roadmap

- **Phase 1 (shipped)**: the nine native `team_*` tools, comment-as-mailbox, per-hash idempotency cache.
- **Phase 2 (shipped)**: `team_operations` journal + durable cross-tool dedup; `team_messages` table (`delivered_at`/`acked_at`/`dead_letter`); `team_debug_bundle` + `team_export_config`; the curated 6 recovery verbs; tool-layer plan-approval enforcement on `team_task_create`; application-level `branch_name` collision hook; `TaskCreated`/`TaskCompleted`/`TeammateIdle` hook registry + pump; board-qualified inbox cursors; `team_spawn agent_type` skill reuse (thin slice).
- **Phase 3 (shipped)**: `worker_pid` SIGTERM-on-timeout in `team_shutdown` (`timeout_seconds`, reuses `reclaim_task`/`_terminate_reclaimed_worker` then `archive_task`); auto-idle on terminal run events (`run_auto_idle_pass`); true push-based delivery via the watcher re-readying recipient tasks.
- **Plugin repackage (shipped)**: the whole feature ships as the installable `agent-teams` plugin (`plugins/agent-teams/`) — all 17 tools register via `register(ctx)`, the 3 tables are plugin-owned (`ensure_tables`, no core schema dependency), and the watcher is re-homed onto the `pre_gateway_dispatch` hook + optional `hermes team-watch` daemon. It depends only on stable vanilla-Hermes core APIs and lives in `~/.hermes/plugins/`, surviving Hermes auto-updates.
