# agent-teams (Hermes plugin)

Claude-parity **agent teams** for Hermes, packaged as a single self-contained,
installable plugin. It gives a team lead a durable Claude-style tool surface — a
named roster, a shared Kanban task board, a comments-as-mailbox, plan-approval
gating, recovery verbs, and an auto-idle + push watcher — without adding anything
to Hermes core.

The plugin registers **18 gated `team_*` tools** via `register(ctx)`, owns its **3
SQLite tables** (created idempotently on connect — no core schema dependency), and
re-homes the watcher onto plugin-safe surfaces (a throttled `pre_gateway_dispatch`
hook plus an optional `hermes team-watch` daemon).

The full operator playbook, tool reference, storage model, and known non-parity
list live in [`SKILL.md`](./SKILL.md).

## Install

Install from a Git repo you control, or copy the directory in by hand:

```bash
hermes plugins install <owner>/<repo>
#   or:
cp -r plugins/agent-teams ~/.hermes/plugins/agent-teams
```

## Enable

1. Enable the plugin in `~/.hermes/config.yaml`:

   ```yaml
   plugins:
     enabled: [agent-teams]
   ```

2. Gate the `team_*` tools on (either is sufficient):

   - `export HERMES_TEAM_LEAD=1` (also accepts `true` / `yes`), **or**
   - add `team` to the active profile's `toolsets:` list.

3. Start a fresh session and confirm the toolset loaded — `/toolsets` should show
   `team [18]`.

## Verify

A runnable real-loader proof ships with the plugin. It installs the plugin into a
throwaway `HERMES_HOME`, loads it through the genuine Hermes plugin loader, asserts
exactly 18 `team_*` tools register, then drops the three tables and runs the full
team lifecycle through the plugin handlers — proving the plugin recreates its own
schema and never relies on a core helper:

```bash
venv/bin/python plugins/agent-teams/tests/verify_install.py   # exit 0 = PASS
```

Run the in-tree unit/gating/lifecycle/runtime suite:

```bash
venv/bin/python -m pytest plugins/agent-teams/tests/ -q
```

## Optional: the `hermes team-watch` daemon

The auto-idle + push passes run two ways:

- **By default** they run *throttled on gateway traffic* via the
  `pre_gateway_dispatch` hook — at most once every 5 seconds, observer-only (it
  never influences dispatch and never raises into the gateway loop). This covers
  active team-lead use, but **will not fire on a fully idle gateway** (no traffic
  → no hook).
- **For true timer-based delivery on an idle gateway**, run the opt-in daemon in
  its own terminal or `tmux` pane:

  ```bash
  hermes team-watch
  ```

  It is a blocking loop that ticks every board with teams every 5 seconds,
  advancing roster auto-idle transitions and pushing addressed messages to
  recipients.

The 18 tools and the 3 tables work fully without either watcher surface (manual
`team_member_update` + `team_inbox` polling is the baseline model), so the feature
is complete even if you never run the daemon.

## Why a plugin: the durability win

Hermes ships its own checkout under `~/.hermes/hermes-agent/` and `git reset
--hard`s it back to vanilla `origin/main` on every self-update. Anything added to
that checkout (core edits, ad-hoc tool files) is wiped on the next update.

This plugin lives in `~/.hermes/plugins/agent-teams/` — **outside** that
self-resetting checkout — and depends only on stable vanilla-Hermes core APIs
(`hermes_cli.kanban_db`). So it **survives Hermes auto-updates**: nothing to
re-apply, nothing to lose.

## Optional cleanup of the legacy core copies

An earlier prototype carried agent-teams as direct **core edits** on the
`agent-teams-phase1` branch (`tools/team_tools.py`, `hermes_cli/kanban_team*.py`,
`gateway/team_notifier.py`, plus `gateway/run.py` / `toolsets.py` wiring). Those
copies are now redundant — the installed plugin is the real implementation. They
can optionally be removed, but you don't have to: Hermes `git reset`s its checkout
to vanilla `origin/main` on update, so the legacy copies self-resolve (disappear)
on the next Hermes update regardless.
