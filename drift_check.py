"""Integrity / drift guard for the agent-teams team modules (AT-01).

Background (audit AT-01): three diverging copies of the team layer were found
in production — the git repo, the user-plugin install at
``~/.hermes/plugins/agent-teams``, and a built-in copy under
``hermes_cli``/``tools`` that dispatcher-spawned workers actually load. The
copies were kept "the same" by hand and the built-in copy is wiped on Hermes
auto-update, so silent behavioral skew was possible with nothing to catch it.

This module is the loud-failure guard the audit recommended. It pins a
committed manifest (:data:`MANIFEST_PATH`, ``MANIFEST.sha256``) of the sha256 of
every team module and offers two checks:

* :func:`verify_integrity` — recompute the hashes of *this package's own*
  module files and compare to the manifest. Catches a local edit that forgot to
  regenerate the manifest (tamper / accidental drift inside the repo or the
  installed copy). Wire into boot to fail loud, or call from CI.
* :func:`compare_tree` — recompute the hashes of an *external* install
  directory (e.g. the live VPS plugin, or the built-in ``hermes_cli`` copy) and
  diff them against this repo's manifest. This is the AT-01 cross-copy check:
  run it in CI/deploy to assert the deployed copy matches the version-controlled
  one before relying on it.

Regenerate the manifest after an intentional change:  ``python -m
plugins.agent_teams.drift_check --write``  (or run this file directly from the
repo root). CI runs ``--check`` (exit non-zero on any mismatch).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# The team modules whose bytes are load-bearing for correctness. Kept explicit
# (not a glob) so adding a module is a deliberate manifest change, and so test
# files / caches / docs never enter the integrity set.
TRACKED_MODULES: Tuple[str, ...] = (
    "__init__.py",
    "kanban_team.py",
    "kanban_team_store.py",
    "kanban_team_runtime.py",
    "kanban_team_recovery.py",
    "kanban_team_hooks.py",
    "team_tools.py",
    "watcher.py",
    "drift_check.py",
)

MANIFEST_NAME = "MANIFEST.sha256"
_PKG_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = _PKG_DIR / MANIFEST_NAME


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compute_hashes(root: Path) -> Dict[str, str]:
    """Return ``{module_name: sha256}`` for every tracked module under ``root``.

    A module absent from ``root`` maps to the sentinel ``"<missing>"`` so a
    deleted/renamed file shows up as a mismatch rather than a silent skip.
    """
    out: Dict[str, str] = {}
    for name in TRACKED_MODULES:
        p = root / name
        out[name] = _sha256(p) if p.is_file() else "<missing>"
    return out


def load_manifest(path: Path = MANIFEST_PATH) -> Dict[str, str]:
    """Parse a ``MANIFEST.sha256`` (``<sha256>  <name>`` lines) into a dict."""
    manifest: Dict[str, str] = {}
    if not path.is_file():
        return manifest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            manifest[parts[-1]] = parts[0]
    return manifest


def render_manifest(hashes: Dict[str, str]) -> str:
    """Render a ``{name: sha256}`` map as ``sha256sum``-compatible text."""
    lines = [
        "# agent-teams team-module integrity manifest (AT-01).",
        "# Regenerate after an intentional change:  python -m "
        "plugins.agent_teams.drift_check --write",
        "# Verify:  python -m plugins.agent_teams.drift_check --check",
    ]
    for name in TRACKED_MODULES:
        lines.append(f"{hashes.get(name, '<missing>')}  {name}")
    return "\n".join(lines) + "\n"


def _diff(
    expected: Dict[str, str], actual: Dict[str, str]
) -> List[Tuple[str, str, str]]:
    """Return ``(name, expected, actual)`` for every tracked module that
    differs between the manifest and a freshly computed set."""
    drift: List[Tuple[str, str, str]] = []
    for name in TRACKED_MODULES:
        exp = expected.get(name, "<unmanifested>")
        act = actual.get(name, "<missing>")
        if exp != act:
            drift.append((name, exp, act))
    return drift


def compare_tree(
    root: Path, manifest_path: Path = MANIFEST_PATH
) -> List[Tuple[str, str, str]]:
    """Diff an external install ``root`` against the repo manifest (AT-01).

    Returns the list of drifting modules as ``(name, expected, actual)``; empty
    means the tree matches the version-controlled copy. Use to assert a
    deployment (the VPS plugin, or the built-in ``hermes_cli`` copy) matches the
    repo before trusting it.
    """
    return _diff(load_manifest(manifest_path), compute_hashes(Path(root)))


def verify_integrity(*, strict: bool = True) -> List[Tuple[str, str, str]]:
    """Check this package's own modules against the committed manifest.

    Returns the list of drifting modules (empty == clean). When ``strict`` and
    drift is found, raises :class:`RuntimeError` with a readable report — call
    this at boot to fail loud, exactly the AT-01 guard. When no manifest exists
    yet (fresh checkout before first ``--write``), returns ``[]`` rather than
    failing, so the guard is opt-in until a manifest is committed.
    """
    # Reference the module-level paths at call time (not via a helper's
    # default arg, which binds once at def-time) so a relocated package — or a
    # test that monkeypatches these — self-checks against the right files.
    manifest = load_manifest(MANIFEST_PATH)
    if not manifest:
        return []
    drift = _diff(manifest, compute_hashes(_PKG_DIR))
    if drift and strict:
        report = "\n".join(
            f"  {name}: manifest={exp[:12]} actual={act[:12]}"
            for name, exp, act in drift
        )
        raise RuntimeError(
            "agent-teams integrity check FAILED (AT-01): the loaded team "
            "modules diverge from the committed MANIFEST.sha256. Either an "
            "edit forgot to regenerate the manifest (run drift_check --write) "
            "or this install was tampered with:\n" + report
        )
    return drift


def _main(argv: List[str]) -> int:
    write = "--write" in argv
    # Allow `--against <dir>` to compare an external tree to the repo manifest.
    against = None
    if "--against" in argv:
        i = argv.index("--against")
        if i + 1 < len(argv):
            against = Path(argv[i + 1])

    if write:
        MANIFEST_PATH.write_text(
            render_manifest(compute_hashes(_PKG_DIR)), encoding="utf-8"
        )
        print(f"wrote {MANIFEST_PATH}")
        return 0

    if against is not None:
        drift = compare_tree(against)
        target = f"tree {against}"
    else:
        drift = verify_integrity(strict=False)
        target = "this package"

    if not drift:
        print(f"agent-teams integrity OK: {target} matches MANIFEST.sha256")
        return 0
    print(f"agent-teams DRIFT in {target} (AT-01):")
    for name, exp, act in drift:
        print(f"  {name}: manifest={exp[:12]} actual={act[:12]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
