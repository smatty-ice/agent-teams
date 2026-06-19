# Hermes Callable — Implementation Plan (MCP first, A2A later)

**Date:** 2026-05-31
**For:** `hermes-vps-2` (Claude resident on the Hetzner VPS, `ssh hermes-vps`, user `hermes`)
**Author role:** planning only — this is a handoff spec. Do not treat any path/port/unit name here as verified; confirm on the box before changing anything.

> **REVISION 2026-05-31 — `hermes-vps-2` did read-only recon (no changes) and
> resolved the blocking questions. Two core recommendations below are now INVERTED;
> sections are annotated inline. Summary of ground truth:**
> - **Ingest (Q1):** Hermes already has a full task system — a **SQLite `kanban.db`
>   with a worker loop inside `hermes gateway run`** (`hermes_cli/kanban_db.py`).
>   Dispatch = `kanban_db.create_task(...)` → `task_id`, status `ready`. Worker
>   `dispatch_once()` (~60s) claims `ready`+unlocked → `running`; `complete_task()`
>   writes `tasks.result`, status `done`, emits `task_events(kind='completed')`.
>   Lifecycle `triage/todo/scheduled/ready/running/blocked/review/done/archived`.
>   Guardrail columns ALREADY exist (`max_runtime_seconds`, `max_retries`,
>   `consecutive_failures`, stale-claim reclaim). **Do NOT build `hermes_tasks` —
>   reuse kanban.** State map: ready→submitted, running→working, blocked→
>   input-required, done→completed, archived→canceled, retries-exhausted→failed.
> - **Bridge (Q3):** `:8000` tools come from **upstream npm packages**
>   (`@agentmemory/mcp@0.9.24`, `@agentmemory/agentmemory@0.9.24` in
>   `/usr/lib/node_modules`). Recommendation (B) would mean forking upstream →
>   **forbidden by this plan's own guardrail → adopt fallback (A): standalone
>   `hermes-mcp-bridge` unit + new `dispatch.smattyice.com` hostname.** (`hermes-vps-2`
>   is checking whether agentmemory ships an official tool-registration/plugin API;
>   if so, (B) revives and we skip the new unit+hostname.)
> - **Net-new work (the real scope):** (a) Hermes has **no HTTP/MCP task surface**
>   today (CLI / `tools/kanban_tools.py` / slash only) — the dispatch bridge is
>   net-new. (b) Hermes core has **no agentmemory integration**; completion routes
>   to chat via `gateway/team_notifier.py` (which already tails `task_events`). So
>   the "report to team feed + memory" half is net-new: a listener modeled on
>   `team_notifier` that, on `kind='completed'` for a dispatched task, does
>   `memory_save` (facet `src:hermes`, `proj:<project>`, ref `task_id`) + team-feed
>   `fyi`.
> - **cloudflared (Q4):** config at `/home/hermes/.cloudflared/config.yml`
>   (user-owned, no sudo). **`rest.smattyice.com → :3111` is already in the ingress**
>   (leftover from a prior hooks attempt — reconcile). Adding `dispatch.smattyice.com`
>   is a one-block edit + `cloudflared` restart (VPS-doable). **BUT the CF Access app
>   for the new hostname is a Cloudflare DASHBOARD action = Winfield-only** — same
>   human blocker the hooks path hit.
> - **Result channel (Q5):** confirmed — agentmemory exposes
>   `signal_send`/`team_feed`/`memory_save`. Ready.
> - **Owner decisions still needed:** Q2 (which assignee/persona dispatched tasks
>   run as), concurrency cap (`dispatch_once` spawns per ready task/tick — find the
>   knob in `config.yaml` or self-limit in the dispatch tool), Q6 values
>   (`hermes-vps-2` recommends CONSERVATIVE: 600s/task, 1 retry, concurrency 1,
>   global pause flag + `hermes_cancel_task` kill switch, caller allowlist
>   `{claude-code,codex,antigravity}` logged).
>
> **Status: nothing built; held at design checkpoint per Winfield.** The body below
> is the original plan; trust the revision box where they conflict.

> **DECISIONS LOCKED 2026-05-31 (Winfield):**
> - **Profile (Q2):** dispatched tasks run as a **dedicated, isolated `dispatch`
>   profile** (NOT `default`) — least privilege, scoped skills/model, so autonomous
>   work from other tools can't reach Hermes's full capability surface. Setup cost
>   accepted for the smaller blast radius. `hermes-vps-2` to define the `dispatch`
>   profile (scoped skills + model) as part of §F build.
> - **Guardrails (Q6):** ACCEPTED as recommended — **600s/task wall-clock, 1 retry,
>   dispatched-concurrency 1** (self-limited; kanban's concurrency knob is board-wide),
>   global pause flag + `hermes_cancel_task` kill switch, caller allowlist
>   `{claude-code,codex,antigravity}` logged.
> - **Approach:** (A) standalone `hermes-mcp-bridge` + `dispatch.smattyice.com` is FINAL
>   (Option B conclusively dead — agentmemory has no custom-tool registration, only
>   `AGENTMEMORY_TOOLS=core|all`).
> - **Next action:** `hermes-vps-2` posts a team-feed note re the addendum, then HOLDS;
>   builds Phase 1 from addendum §F in one pass once the CF Access app exists.
> - **✅ DONE — CF Access app created (Winfield, 2026-05-31):** app `Hermes-Dispatch`,
>   hostname `dispatch.smattyice.com` (renamed from `hermes.` → `dispatch.`), Action
>   **Service Auth**, Include **Service Token `local-mcp-clients`** (same token as
>   `mcp.smattyice.com` — foreground tools need no new creds), browser RDP/SSH/VNC
>   off, MFA/justification/temp-auth off, path blank (whole-hostname). This was the
>   sole remaining gate — **Phase 1 build is now unblocked** once the tunnel ingress
>   block (above) is applied on the box.
> - **OPEN — VPS (`hermes-vps-2`):** apply the ingress block, stand up
>   `hermes-mcp-bridge` on the matching port, then build Phase 1 from addendum §F.
>   Also reconcile the leftover `rest.smattyice.com → :3111` ingress.

---

## 1. Goal & Success Criteria

"Hermes is callable" means: one of the three foreground tools (`claude-code`, `codex`, `antigravity`) can **dispatch a background/long-running task to Hermes** without blocking, and later observe the result through the **existing shared substrate** (agentmemory team feed + tagged result memory). Hermes is the only always-on, owner-controlled agent; the others offload to it.

We do NOT invent a new result channel. Dispatch in (new), results out (reuse the companion handoff design: `signal_send`/`team_feed` + `memory_save` tagged `src:hermes proj:<project>`).

**Acceptance test (Phase 1):**
1. From a laptop holding the Cloudflare Access service token, call the MCP tool `hermes_dispatch_task` with `{project, prompt}`.
2. Tool returns a `task_id` within ~2s (does NOT wait for the work).
3. Hermes's loop picks up the task, runs it.
4. On completion Hermes emits a `fyi` to the team feed AND writes a result memory tagged `src:hermes proj:<project>` referencing `task_id`.
5. Caller polls `hermes_task_status(task_id)` and sees `submitted → working → completed`; the result memory is recallable via `memory_recall`.

**Acceptance test (Phase 2):** any third-party A2A client fetches `/.well-known/agent-card.json` for Hermes, calls `message/send`, and the task flows through the SAME internal task model and surfaces the SAME way (team feed + memory).

---

## 2. Phase 1 — MCP-first (near-term, pragmatic)

### 2.1 Architecture decision: extend the existing agentmemory MCP surface (recommended)

Two options:

- **(A) New standalone Hermes MCP server** — new bridge process, new systemd unit, new CF hostname (`dispatch.smattyice.com/mcp`), new tunnel ingress.
- **(B) Add `hermes_*` dispatch tools into the existing agentmemory MCP server** on `:8000`, reachable over the existing `mcp.smattyice.com/mcp` hostname the foreground tools already trust.

**Recommendation: (B) for Phase 1.** Rationale:
- The three foreground tools are *already* MCP clients of `mcp.smattyice.com/mcp` with the service token configured. Adding tools to that surface = **zero new client wiring** and zero new auth setup.
- Results already live in agentmemory; dispatch + results in one surface keeps the mental model clean.
- No new tunnel ingress / CF Access app / firewall rule to get wrong.

Tradeoff: it couples Hermes-dispatch to the agentmemory process lifecycle and slightly muddies separation of concerns. Acceptable now; Phase 2's A2A server becomes the clean standalone front door later. **Keep the dispatch logic in a thin module** so it can be lifted into a standalone server without rewrite.

> Implementer must first confirm whether the agentmemory MCP bridge on `:8000` is a process you can add tools to (its own codebase) or an opaque packaged binary. If opaque/unmodifiable, fall back to (A): a small new MCP bridge unit on a separate localhost port, fronted by a NEW CF hostname `dispatch.smattyice.com` with the same service-token Access policy. Do not bolt onto `:8000` if it means forking an upstream package.

### 2.2 Tools to expose

- `hermes_dispatch_task(project: str, prompt: str, priority?: "low|normal|high", deadline?: ISO8601, context_refs?: [memory_id]) -> {task_id, state}`
  Enqueues into Hermes's loop. Returns immediately with `task_id` and `state="submitted"`. Stamps provenance: the task is owned by `src:hermes` for its eventual output, tagged `proj:<project>`.
- `hermes_task_status(task_id: str) -> {task_id, state, started_at?, finished_at?, result_memory_id?, error?}`
  Reads task state. States mirror A2A (Section 3) so Phase 2 needs no new vocabulary: `submitted/working/input-required/completed/canceled/failed/rejected`.
- `hermes_cancel_task(task_id: str) -> {task_id, state}` — cooperative cancel; sets a flag the loop checks.
- (optional) `hermes_list_tasks(project?: str, state?: str) -> [task summaries]`.

### 2.3 Async model (the core contract)

```
foreground tool ──hermes_dispatch_task──▶ agentmemory MCP (:8000)
                                            │ writes task row (state=submitted)
                                            │ enqueues for Hermes loop
                                            ▼
                                   Hermes loop polls queue ──▶ state=working
                                            │ does the work
                                            ▼
                          on done: signal_send kind=fyi  (team feed)
                                   memory_save result, tags: src:hermes proj:<project>, ref task_id
                                   task row → state=completed, result_memory_id set
foreground tool ──hermes_task_status──▶ sees completed + result_memory_id
                  ──memory_recall──▶ picks up the actual result
```

Dispatch never blocks on the work. The queue can be a table in agentmemory's existing store (a `hermes_tasks` table) or a small JSON/SQLite queue the Hermes loop already reads — implementer decides based on how the loop currently ingests work (open question Q1). Hermes's existing plan to emit `fyi` on result deposit satisfies the "report back" half with no new code path.

### 2.4 systemd + cloudflared changes

Under recommendation (B), ideally **none** at the infra layer: same `agentmemory-mcp-bridge` unit, same `:8000`, same `cloudflared-agentmemory` tunnel, same `mcp.smattyice.com` ingress. You add tools inside the bridge and (likely) a queue-read into the Hermes stack unit.

- If the queue is a new table in agentmemory's DB: only the agentmemory bridge code changes; `systemctl restart agentmemory-mcp-bridge` after deploy.
- If Hermes needs to poll a new source: edit the Hermes stack unit's program or config, `daemon-reload` only if the unit file itself changes, then restart the Hermes unit.
- Under fallback (A) only: add `hermes-mcp-bridge.service` (bare node process, `User=hermes`, localhost port e.g. `:8011`), add a `cloudflared` ingress rule mapping `dispatch.smattyice.com → http://localhost:8011`, restart `cloudflared-agentmemory`, and create a CF Access app for `dispatch.smattyice.com` reusing the existing service-token policy.

#### Concrete cloudflared ingress block (ready to paste)

Edit `/home/hermes/.cloudflared/config.yml`. The new rule MUST sit above the
catch-all `- service: http_status:404` (cloudflared matches top-to-bottom). `8011`
is a placeholder — bind the `hermes-mcp-bridge` unit to a confirmed-free port
(`ss -ltnp`) and keep the two in sync.

```yaml
ingress:
  - hostname: mcp.smattyice.com
    service: http://localhost:8000
  - hostname: memory.smattyice.com        # Anthropic-IP-only path — DO NOT TOUCH
    service: http://localhost:8000
  - hostname: rest.smattyice.com          # leftover from prior hooks attempt — reconcile/remove if unused
    service: http://localhost:3111
  - hostname: dispatch.smattyice.com       # NEW — Hermes dispatch bridge
    service: http://localhost:8011         # == hermes-mcp-bridge bind port
  - service: http_status:404               # catch-all MUST stay last
```

Before reload: `cloudflared tunnel ingress validate`. Then
`systemctl restart cloudflared-agentmemory` (confirm unit name). The CF Access app
for `dispatch.smattyice.com` (Service Auth + `local-mcp-clients` token) must already
exist so the hostname is never briefly open.

Stay bare-systemd. No Docker.

### 2.5 Auth & authorization

- Reuse the **Cloudflare Access service token** pattern. Under (B) the transport auth is already done — anyone who can call agentmemory can call `hermes_dispatch_task`.
- Because dispatch launches autonomous work (cost + side effects), add an **app-level allowlist**: dispatch tools accept only callers whose provenance is one of `claude-code|codex|antigravity` (or any token, but require a `caller_tool` arg and log it). Keep it simple now; the trust boundary is the service token.
- Log every dispatch (caller, project, prompt hash, task_id) for auditability and cost tracing.

---

## 3. Phase 2 — A2A (standards-based, externally interoperable)

Goal: any A2A client (not just the owner's tools) can discover and call Hermes. A2A is v1.0.1 (Linux Foundation, Apache-2.0).

### 3.1 AgentCard

Serve at `https://dispatch.smattyice.com/.well-known/agent-card.json` (Phase 2 justifies the dedicated hostname even if Phase 1 reused `mcp.`). Contents:

```json
{
  "name": "Hermes",
  "description": "Always-on autonomous background agent. Accepts long-running tasks and reports results via shared memory.",
  "url": "https://dispatch.smattyice.com",
  "version": "1.0.0",
  "preferredTransport": "JSONRPC",
  "capabilities": { "streaming": true, "pushNotifications": true },
  "skills": [
    { "id": "background_task", "name": "Run background task",
      "description": "Execute a long-running prompt/task autonomously and deposit results to shared memory.",
      "tags": ["background","autonomous","research","coding"] }
  ],
  "securitySchemes": { "serviceToken": { "type": "apiKey", "in": "header", "name": "CF-Access-Client-Id" } }
}
```

Optionally JWS-sign the card later. Skills can be enumerated more granularly once Hermes's real capability set is confirmed (Q2).

### 3.2 Transport: JSON-RPC 2.0 over HTTP first, via `a2a-sdk`

`pip install a2a-sdk`. Implement **JSON-RPC 2.0 over HTTP** first (simplest, matches MCP's JSON-RPC mental model). Defer gRPC/REST. Map core ops onto the internal task model from Phase 1:

| A2A op | Hermes internal |
|---|---|
| `message/send` | create task (= `hermes_dispatch_task`), return Task `submitted` |
| `message/stream` (SSE) | stream loop state transitions |
| `tasks/get` | `hermes_task_status` |
| `tasks/cancel` | `hermes_cancel_task` |
| `tasks/pushNotificationConfig/*` | optional webhook on completion |

A2A lifecycle states (`submitted/working/input-required/completed/canceled/failed/rejected/auth-required`) are exactly what Phase 1 task states were defined as — deliberately. **One internal task model, two front doors.**

### 3.3 Same result path

A2A tasks do NOT get a separate result channel. On completion they ALSO emit team-feed `fyi` + write `src:hermes proj:<project>` memory. A2A's `tasks/get`/SSE return task status + a pointer (`result_memory_id`) into shared memory; the canonical result lives in agentmemory. This keeps owner tools and external A2A clients converging on the same store.

### 3.4 Auth & the trust caveat

- Phase 2 transport auth: reuse the CF Access service-token scheme (declare it in `securitySchemes`). For truly external A2A callers, decide later whether to issue a scoped token; do not open Hermes to unauthenticated dispatch.
- **Caveat (A2A spec gap):** A2A has **no native inter-agent delegation / trust chain** — Hermes can't cryptographically verify "agent X is acting on behalf of user Y." Treat every A2A caller as a flat principal gated only by its token. **Caveat:** A2A streaming has **no resume primitive** — a dropped SSE connection means re-`tasks/get`, not resume. Document both for callers.

---

## 4. Sequencing, Risks, Rollback

**Order of operations:**
1. (Q1–Q4 first.) Inspect Hermes loop ingestion, agentmemory bridge editability, current `cloudflared` config, CF Access policies.
2. Add `hermes_tasks` store + dispatch/status/cancel tools to the agentmemory MCP surface (recommendation B), keeping logic in a liftable module.
3. Wire Hermes loop to consume the queue and, on completion, emit `fyi` + tagged result memory + update task row.
4. Run Phase 1 acceptance test from a laptop.
5. Only then: stand up `dispatch.smattyice.com` + A2A server (`a2a-sdk`, JSON-RPC), serve AgentCard, bridge ops to the same internal task model.
6. Run Phase 2 acceptance test with a generic A2A client.

**Risks & rollback:**
- **Port/tunnel conflict on `:8000`** — under (B) you don't touch the port; under (A) verify the new localhost port is free (`ss -ltnp`) before binding. Rollback: remove the new ingress rule, `systemctl restart cloudflared-agentmemory`.
- **Firewall regression** — do NOT touch `memory.smattyice.com`'s Anthropic-IP-only rule; a new hostname must get its OWN Access app. Rollback: delete the new CF Access app/ingress.
- **Runaway autonomous tasks (cost/side effects)** — add per-task wall-clock + step caps, a global concurrency cap on the Hermes loop, and a kill switch (`hermes_cancel_task` + a global pause flag). Rollback for a bad deploy: revert tool registration, `systemctl restart` the bridge; the queue table is additive and safe to leave.
- **Schema/queue corruption** — make the `hermes_tasks` store additive; never migrate agentmemory's existing tables destructively. Back up the DB file before first deploy.
- **Bridge restart drops in-flight MCP sessions** — restart during a quiet window; foreground tools reconnect.

`hermes-vps-2` MUST verify on the box before changing anything: current unit names/ports (`systemctl --user list-units` and system units), `cloudflared` config file location + ingress rules, the agentmemory DB path, and that `:8000` is the bridge.

---

## 5. Open Questions — status after `hermes-vps-2` recon (2026-05-31)

- **Q1 — Hermes ingest. ✅ RESOLVED.** SQLite `kanban.db` + worker loop in
  `hermes gateway run` (`hermes_cli/kanban_db.py`). Dispatch = `create_task()`;
  reuse it, don't build a new queue. (See revision box.)
- **Q2 — Hermes capabilities/skills + dispatch persona. ⚠️ OWNER DECISION.** Which
  assignee/persona do dispatched tasks run as — reuse `default`, or a dedicated
  `dispatch` persona with scoped skills/model? Needed for AgentCard `skills[]` and
  safety scoping. **Winfield to decide.**
- **Q3 — Bridge editability. ✅ RESOLVED → fallback (A).** `:8000` tools are upstream
  npm packages; do not fork. Stand up a standalone `hermes-mcp-bridge` + new
  `dispatch.smattyice.com`. (Pending: does agentmemory ship a plugin/tool-registration
  API? If yes, (B) revives — `hermes-vps-2` checking.)
- **Q4 — cloudflared / CF Access. ◑ PARTIAL.** Config at
  `/home/hermes/.cloudflared/config.yml` (user-owned). Adding the ingress block is
  VPS-doable; **the CF Access app for `dispatch.smattyice.com` is dashboard-only =
  Winfield action.** Also reconcile the existing `rest.smattyice.com → :3111` rule.
- **Q5 — Result channel API. ✅ RESOLVED.** `signal_send` / `team_feed` / `memory_save`
  confirmed present.
- **Q6 — Guardrails. ◑ RECOMMENDED, owner to confirm.** `hermes-vps-2` proposes
  conservative defaults: 600s/task, 1 retry, concurrency 1, global pause flag +
  `hermes_cancel_task` kill switch, caller allowlist `{claude-code,codex,antigravity}`
  logged. kanban already has the guardrail columns, so these are mostly values to set.

**Net-new build scope (the actual work, now that ingest exists):**
1. A dispatch surface (MCP tools, then A2A) that calls `kanban_db.create_task` / reads
   task rows / cancels — Hermes has no HTTP/MCP task surface today.
2. An agentmemory **completion listener** modeled on `gateway/team_notifier.py` (which
   already tails `task_events`): on `kind='completed'` for a dispatched task →
   `memory_save` (facet `src:hermes`, `proj:<project>`, ref `task_id`) + team-feed `fyi`.
3. The standalone bridge unit + `dispatch.smattyice.com` (gated on the Winfield-only CF
   Access app).

**Two human-only blockers for Winfield:** (a) create the CF Access app for the new
hostname; (b) decide the dispatch persona (Q2) and confirm the Q6 guardrail values.
