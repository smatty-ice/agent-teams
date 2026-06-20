#!/usr/bin/env python3
"""Real-loader install verification + self-containment proof for agent-teams.

This is a RUNNABLE SCRIPT (not a pytest). It proves two things that the
in-tree pytest suite — which loads the package via a conftest shim — cannot:

  1. REAL LOADER PATH. We install the plugin into a throwaway ``HERMES_HOME``,
     enable it in ``config.yaml``, and load it through the genuine Hermes
     plugin loader entrypoint:

         hermes_cli.plugins.discover_plugins(force=True)
           -> get_plugin_manager().discover_and_load(force=True)   # plugins.py
              -> scans $HERMES_HOME/plugins, respects plugins.enabled,
                 imports each __init__.py as hermes_plugins.<slug>,
                 calls register(ctx) -> ctx.register_tool -> registry.register

     Then we resolve the ``team`` toolset via the same surface the live agent
     uses (model_tools.get_tool_definitions) and assert exactly 18 ``team_*``
     tools registered THROUGH the real loader (with HERMES_TEAM_LEAD=1).

  2. SELF-CONTAINMENT. The user's real Hermes runs vanilla ``origin/main``,
     which NEVER had our three team_* tables or kb.list_comments_after. We
     simulate that exactly: after kb.init_db(), we DROP the three team_* tables
     that this branch's core still creates, then run the full lifecycle smoke
     ENTIRELY through the plugin's tool handlers. If the smoke passes with
     core's tables gone, the plugin is proven self-contained — it recreates its
     own schema (ensure_tables) and never relies on a core helper we added.

Run:  venv/bin/python plugins/agent-teams/tests/verify_install.py
Exit: 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# The 18 tools the plugin must provide.
EXPECTED_TEAM_TOOLS = {
    "team_create", "team_spawn", "team_task_create",
    "team_send", "team_inbox", "team_inbox_ack",
    "team_status", "team_list", "team_shutdown", "team_delete",
    "team_debug_bundle", "team_export_config",
    "team_inspect", "team_replay", "team_requeue",
    "team_mark_blocked", "team_restore_assignment", "team_explain_blockage",
}

# The source plugin dir = this file's grandparent (tests/ -> plugin dir). Works
# BOTH standalone (~/Projects/agent-teams) and in a <hermes>/plugins/agent-teams
# checkout. hermes_cli is provided by the Hermes venv this is run with.
_THIS = Path(__file__).resolve()
SRC_PLUGIN = _THIS.parents[1]
REPO_ROOT = SRC_PLUGIN


class Failure(Exception):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _setup_hermes_home(tmp: Path) -> Path:
    """Create $HERMES_HOME/plugins, copy the plugin in, enable it in config."""
    home = tmp / ".hermes"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(
        SRC_PLUGIN,
        home / "plugins" / "agent-teams",
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache", "tests"
        ),
    )
    import yaml
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["agent-teams"]}})
    )
    return home


def verify_real_loader() -> list[str]:
    """Load the plugin through the REAL PluginManager and return the team_*
    tool names resolved via get_tool_definitions. Asserts exactly 18."""
    # The real loader entrypoint. discover_plugins(force=True) scans
    # $HERMES_HOME/plugins, honors plugins.enabled, and runs register(ctx).
    from hermes_cli import plugins as P
    from tools.registry import invalidate_check_fn_cache
    from model_tools import get_tool_definitions

    P.discover_plugins(force=True)

    mgr = P.get_plugin_manager()
    loaded = mgr._plugins.get("agent-teams")
    _check(loaded is not None, "agent-teams not discovered by the real loader")
    _check(
        loaded.enabled,
        f"plugin failed to load: error={getattr(loaded, 'error', None)!r}",
    )
    # The module imported under the real slug (hyphen -> underscore).
    _check(
        "hermes_plugins.agent_teams" in sys.modules,
        "plugin not imported under the real loader slug hermes_plugins.agent_teams; "
        f"got: {[m for m in sys.modules if 'agent_teams' in m or 'agent-teams' in m]}",
    )

    invalidate_check_fn_cache()
    schemas = get_tool_definitions(enabled_toolsets=["team"], quiet_mode=True)
    names = sorted(
        s["function"]["name"]
        for s in schemas
        if "function" in s and s["function"]["name"].startswith("team_")
    )
    _check(
        set(names) == EXPECTED_TEAM_TOOLS,
        f"expected the 18 team tools, got {len(names)}: {names}\n"
        f"  missing: {sorted(EXPECTED_TEAM_TOOLS - set(names))}\n"
        f"  extra:   {sorted(set(names) - EXPECTED_TEAM_TOOLS)}",
    )
    return names


def verify_self_containment() -> dict:
    """Drop core's three team_* tables (simulating vanilla origin/main), then
    run the full lifecycle smoke through the plugin's tool HANDLERS. Returns a
    summary dict. Raises Failure on any assertion miss."""
    from hermes_cli import kanban_db as kb
    # The plugin handlers, imported via the real loader slug — this is the
    # module the live agent dispatches into.
    import hermes_plugins.agent_teams.team_tools as tt
    import hermes_plugins.agent_teams.kanban_team as kt

    # Confirm the plugin never references kb.list_comments_after (a core helper
    # we added that vanilla Hermes lacks). Static guard against regression.
    src = (SRC_PLUGIN / "kanban_team.py").read_text()
    _check(
        "kb.list_comments_after" not in src,
        "plugin references kb.list_comments_after — NOT self-contained "
        "(vanilla core lacks it; use the plugin-local kts.list_comments_after)",
    )

    # ---- Simulate vanilla origin/main, which NEVER had the team_* tables ----
    #
    # This branch's core kanban_db ships the three team_* tables inside
    # SCHEMA_SQL, which ``connect()`` re-applies on every fresh-path init. So
    # merely DROPping them isn't enough — core would recreate them on the next
    # connect, and the smoke would pass for the WRONG reason (core, not the
    # plugin, owning the schema). To faithfully reproduce vanilla core we strip
    # the three ``CREATE TABLE ... team_*`` statements out of ``kb.SCHEMA_SQL``
    # for the duration of the smoke, then clear the init cache so the trimmed
    # schema is what gets laid down. After this, ONLY the plugin's
    # ensure_tables() (called inside the tool handlers' _conn()) can create the
    # tables — exactly the production code path on a real vanilla install.
    import re
    original_schema = kb.SCHEMA_SQL
    trimmed = original_schema
    for tbl in ("team_operations", "team_messages", "team_hook_subs"):
        # Remove the whole `CREATE TABLE IF NOT EXISTS <tbl> ( ... );` block.
        trimmed = re.sub(
            rf"CREATE TABLE IF NOT EXISTS {tbl}\s*\(.*?\);",
            "",
            trimmed,
            flags=re.DOTALL,
        )
    # On modern Hermes core, SCHEMA_SQL no longer ships the team_* tables at all
    # (the plugin owns them), so there may be nothing to strip — and that IS the
    # vanilla-core state we want to reproduce, not a failure. Either way, the real
    # proof is the post-init assertion below (a fresh connect sees none of the
    # three tables) plus the lifecycle smoke (only the plugin can recreate them).
    if trimmed == original_schema:
        print(
            "  note: core SCHEMA_SQL already lacks team_* DDL (modern core) — "
            "vanilla state is intrinsic, not simulated"
        )
    kb.SCHEMA_SQL = trimmed
    try:
        # Re-init from scratch so the trimmed (vanilla-shaped) schema applies.
        try:
            kb._INITIALIZED_PATHS.clear()
        except AttributeError:
            pass
        kb.init_db()

        # Drop any straggler copies, then PROVE vanilla state: a fresh
        # connection sees none of the three tables (as on real origin/main).
        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                for tbl in ("team_operations", "team_messages", "team_hook_subs"):
                    conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        finally:
            conn.close()
        conn = kb.connect()
        try:
            present = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('team_operations','team_messages','team_hook_subs')"
                ).fetchall()
            }
            _check(
                present == set(),
                f"vanilla-core simulation failed: core still recreates {present}",
            )
        finally:
            conn.close()

        return _run_lifecycle_smoke(kb, tt, kt)
    finally:
        kb.SCHEMA_SQL = original_schema


def _run_lifecycle_smoke(kb, tt, kt) -> dict:
    """Run the full create->...->delete lifecycle entirely through the plugin's
    tool handlers (the live dispatch path) and assert every contract. Called
    with the three team_* tables ABSENT from core schema, so a pass proves the
    plugin recreates and owns its own schema (ensure_tables inside _conn())."""
    # --- lifecycle smoke, entirely via tool handlers (the live dispatch path) ---
    def call(handler, args):
        out = handler(args)
        payload = json.loads(out)
        if isinstance(payload, dict) and payload.get("error"):
            raise Failure(f"{handler.__name__} returned error: {payload['error']}")
        return payload

    created = call(tt._handle_team_create, {"name": "verify", "goal": "prove it"})
    team_id = created["id"]

    spawned = call(tt._handle_team_spawn, {
        "team_id": team_id, "member_name": "alice", "role": "researcher",
        "profile": "p-alice", "prompt": "do research",
    })
    member_task_id = spawned["task_id"]

    call(tt._handle_team_send, {
        "team_id": team_id, "to": "alice", "sender": "lead",
        "message": "kick off the research",
    })

    inbox = call(tt._handle_team_inbox, {
        "team_id": team_id, "member": "alice", "unread_only": False,
    })

    work_task = call(tt._handle_team_task_create, {
        "team_id": team_id, "title": "Write the report", "owner": "alice",
    })

    status = call(tt._handle_team_status, {"team_id": team_id})

    bundle = call(tt._handle_team_debug_bundle, {"team_id": team_id})

    # ---- PRE-DELETE assertions on the live member task ----
    # Capture the member task's spawned state BEFORE team_delete archives it.
    conn = kb.connect()
    try:
        member_task = kb.get_task(conn, member_task_id)
        _check(member_task is not None, "spawned member task vanished")
        _check(
            member_task.status == "ready",
            f"member task status: expected 'ready', got {member_task.status!r}",
        )
        _check(
            member_task.tenant == "team:verify",
            f"member task tenant: expected 'team:verify', got {member_task.tenant!r}",
        )
        _check(
            (member_task.body or "").startswith("[owner:@alice]"),
            f"member task body must start with owner header; got "
            f"{(member_task.body or '')[:40]!r}",
        )
    finally:
        conn.close()

    # Inbox returned the sent message.
    _check(
        any("kick off the research" in (m.get("body") or "") for m in inbox),
        f"inbox did not return the sent message: {inbox}",
    )
    # team_status counts the one work task.
    _check(
        status["tasks"]["total"] == 1,
        f"team_status work-task total: expected 1, got {status['tasks']['total']}",
    )
    # debug bundle round-trips identity (proves team_operations/messages
    # tables — recreated by the plugin — are readable).
    _check(
        bundle["team"]["name"] == "verify",
        f"debug bundle team name mismatch: {bundle['team'].get('name')!r}",
    )

    # team_delete with the member still active needs force=True.
    deleted = call(tt._handle_team_delete, {"team_id": team_id, "force": True})

    # ---- POST-DELETE assertions: root AND member task archived ----
    conn = kb.connect()
    try:
        member_task = kb.get_task(conn, member_task_id)
        root_task = kb.get_task(conn, team_id)
        _check(
            root_task is not None and root_task.status == "archived",
            f"root task not archived after delete: "
            f"{None if root_task is None else root_task.status!r}",
        )
        _check(
            member_task is not None and member_task.status == "archived",
            f"member task not archived after delete: "
            f"{None if member_task is None else member_task.status!r}",
        )
    finally:
        conn.close()

    return {
        "team_id": team_id,
        "member_task_id": member_task_id,
        "work_task_id": work_task["task_id"],
        "deleted": deleted.get("deleted"),
        "inbox_count": len(inbox),
    }


def main() -> int:
    # Set HERMES_HOME (and the lead gate) BEFORE importing any hermes module so
    # the real loader, config, and kanban DB all root at the temp home.
    tmp = Path(tempfile.mkdtemp(prefix="agent-teams-verify-"))
    print("=" * 72)
    print("agent-teams plugin — real-loader install verification")
    print("=" * 72)
    print(f"temp HERMES_HOME: {tmp / '.hermes'}")

    results: list[tuple[str, bool, str]] = []
    try:
        # config.yaml + plugin copy must exist before any import reads the home.
        os.environ["HERMES_HOME"] = str(tmp / ".hermes")
        os.environ["HERMES_TEAM_LEAD"] = "1"
        # Make the repo importable (run-from-anywhere).
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))

        home = _setup_hermes_home(tmp)
        print(f"installed plugin -> {home / 'plugins' / 'agent-teams'}")
        print(f"enabled in       -> {home / 'config.yaml'}")
        print()

        # --- Check 1: real loader registers exactly 18 tools ---
        try:
            names = verify_real_loader()
            print(f"[1] REAL LOADER: discover_plugins(force=True) loaded the "
                  f"plugin and registered {len(names)} team_* tools:")
            for n in sorted(names):
                print(f"      - {n}")
            results.append(("real-loader 18-tool registration", True, ""))
        except Failure as e:
            results.append(("real-loader 18-tool registration", False, str(e)))

        print()

        # --- Check 2: self-containment with core tables dropped ---
        try:
            summary = verify_self_containment()
            print("[2] SELF-CONTAINMENT: stripped the 3 team_* tables from core")
            print("    SCHEMA_SQL + dropped them (simulating vanilla origin/main,")
            print("    which never had them), then ran")
            print("    create->spawn->send->inbox->task_create->status->"
                  "debug_bundle->delete")
            print("    entirely through the plugin's tool handlers:")
            print(f"      team_id        = {summary['team_id']}")
            print(f"      member task    = {summary['member_task_id']} "
                  f"(ready, tenant=team:verify, [owner:@alice] header)")
            print(f"      inbox returned = {summary['inbox_count']} message(s)")
            print(f"      delete archived root + member task")
            results.append(("self-containment smoke (core tables dropped)", True, ""))
        except Failure as e:
            results.append(("self-containment smoke (core tables dropped)", False, str(e)))

    except Exception:
        print("UNEXPECTED ERROR during verification:")
        traceback.print_exc()
        results.append(("setup", False, "unexpected exception (see traceback)"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- summary ---
    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    all_ok = True
    for label, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  [{mark}] {label}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")
    print("-" * 72)
    print(f"OVERALL: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
