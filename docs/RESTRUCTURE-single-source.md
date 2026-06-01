# Single-Source-of-Truth Restructure Proposal (AT-01 / AT-02)

**Status:** proposal (P4 deliverable — not yet executed)
**Audience:** Hermes maintainer / whoever owns the `hermes-agent` repo and the VPS deploy
**Origin:** ecosystem-audit 2026-06-01, findings AT-01 (HIGH) and AT-02 (MEDIUM) in `Audits/findings/agent-teams.md`

## 1. The problem

The agent-teams team layer exists as **three diverging copies** on the production box, with no mechanism keeping them equivalent:

| Copy | Path | Role |
|---|---|---|
| Repo (this repo, git HEAD) | `~/Projects/agent-teams` → `github.com/smatty-ice/agent-teams` | version-controlled source; what the test suite (105 cases) validates |
| User-plugin install | `~/.hermes/plugins/agent-teams` | config-enabled plugin (`plugins.enabled: [agent-teams]`); registers all 17 tools via `register(ctx)` |
| **Built-in core copy** | `~/.hermes/hermes-agent/hermes_cli/kanban_team*.py` + `tools/team_tools.py` | **the copy dispatcher-spawned workers actually load**; `gateway/team_notifier.py` imports it for the auto-idle/push watcher |

Two compounding hazards:

- **AT-01 — drift.** The built-in core copy is hand-synced from the repo and, per its own in-source comment, is *"git-reset on Hermes auto-update — re-apply after updating."* So the deployed behavior depends on a human re-applying edits after every update, and a hand-edit to core (e.g. the `HERMES_KANBAN_TASK` teams-viewer stamp) is invisible to git and lost on the next update. The audited+tested artifact ≠ the deployed artifact.
- **AT-02 — silent registration collision.** Both `tools/team_tools.py` (built-in, self-registers at import) and the plugin's `register(ctx)` register the same 17 names under the **same** `"team"` toolset. `tools/registry.py:register()` only rejects a *cross-toolset* shadow; a same-toolset re-registration is a silent last-write-wins overwrite — no error, no log. Which copy backs a tool call is import-order-dependent, and nothing records the swap.

## 2. Goal

One source of truth for the team layer, delivered to every consumer (tool registration **and** the gateway watcher) from the **version-controlled** copy, surviving Hermes auto-update, with drift caught loudly rather than discovered in production.

## 3. Options considered

### Option A — Plugin is the only copy (recommended)
Delete the built-in `hermes_cli/kanban_team*.py` + `tools/team_tools.py`. The `agent-teams` *plugin* (this repo) becomes the sole implementation; dispatcher-spawned workers load the team toolset via the normal plugin path, and `gateway/team_notifier.py` imports the runtime from the plugin instead of `hermes_cli`.

- **Pro:** matches the original intent — the plugin's `team_tools.py` docstring already says the module-level `registry.register` loop *"was removed when this moved from core `tools/` into the plugin."* The move was started; this finishes it. Eliminates AT-01 and AT-02 at the root.
- **Con:** requires the gateway watcher to depend on a plugin package (`team_notifier` must import the plugin module, or the watcher must move fully behind the plugin's `pre_gateway_dispatch` hook + `team-watch` daemon, which already exist). Needs confirmation that dispatcher workers reliably load user plugins (they load the `team` toolset today, so the gating path is present).

### Option B — Core is the only copy, vendored from the repo at deploy
Keep the built-in copy as the single implementation, delete the separate user-plugin install, and **generate** `hermes_cli/kanban_team*.py` + `tools/team_tools.py` from this repo at build/deploy time (git submodule, vendoring step, or symlink into the hermes-agent tree) so they are never hand-edited and the deploy re-applies them after every auto-update.

- **Pro:** no change to how workers/gateway import the team layer (still `hermes_cli`). Removes the "re-apply by hand" ritual.
- **Con:** keeps a build-time copy step; the repo's package-relative imports (`from . import kanban_team_store`) differ from core's absolute imports (`from hermes_cli import kanban_team_store`), so vendoring needs an import-rewrite or a shared shim. Does not by itself fix AT-02 unless the user-plugin install is also removed.

### Option C — Keep both, make drift + collision loud (interim, implemented now)
Do not restructure yet, but (a) detect+log the same-toolset registration collision, and (b) add a checksum manifest so any divergence between the deployed copy and the repo is caught by CI/boot. This is the **P4 stopgap shipped in this branch** — it does not remove the duplication (that is P5/maintainer scope), it makes the duplication safe to live with until A or B lands.

## 4. Recommendation

**Adopt Option A.** It is the only option that removes both the duplicate implementation and the duplicate registration, and it completes a migration the codebase already started. Sequence:

1. Land the interim guards (this branch): AT-02 collision logging in `__init__.py`, AT-01 `drift_check.py` + `MANIFEST.sha256`. (Done.)
2. In the hermes-agent repo: point `gateway/team_notifier.py` at the plugin's `kanban_team_runtime` (or retire the in-gateway watcher in favor of the plugin's existing `pre_gateway_dispatch` hook + `team-watch` daemon).
3. Confirm dispatcher-spawned workers load the `agent-teams` plugin's `team` toolset (add a boot assertion / test).
4. Delete `hermes_cli/kanban_team*.py` and `tools/team_tools.py` from hermes-agent.
5. Add a CI gate that runs `python -m plugins.agent_teams.drift_check --against <deployed-path>` so a deploy fails if the installed copy ≠ the repo.

## 5. Interim guards shipped in this branch (Option C)

- **AT-02:** `__init__.register()` now calls `_warn_on_existing_team_tool(name)` before each `ctx.register_tool`, logging a WARNING when a `team_*` name is already registered under the `"team"` toolset — so the otherwise-silent overwrite is auditable in `agent.log`. The deeper fix (make `tools/registry.register` itself log same-toolset overwrites) belongs to the hermes-agent repo that owns `registry.py` and is out of scope for this repo.
- **AT-01:** `drift_check.py` pins a committed `MANIFEST.sha256` of the team modules. `verify_integrity()` (call at boot) fails loud if the loaded modules diverge from the manifest; `compare_tree(<install-dir>)` / `--against <dir>` diffs an external deployment (the VPS plugin, or the built-in core copy) against the repo manifest for CI. Regenerate with `--write` after any intentional change.

## 6. Out of scope here

Editing `hermes-agent` (`tools/registry.py`, `gateway/team_notifier.py`, deletion of the core copy) and any live-VPS change are **not** part of this repo's P4 branch — they are P5/maintainer actions. This document is the plan; the code in this branch is only the in-repo interim guards plus the security/parity fixes (AT-03, AT-04).
