"""Tests for drift_check — the AT-01 team-module integrity guard.

Verifies the committed manifest matches the live package, that tampering is
detected, and that an external install tree can be diffed against the repo
manifest (the cross-copy AT-01 check).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from plugins.agent_teams import drift_check as dc


def test_committed_manifest_matches_package():
    """The MANIFEST.sha256 checked into the repo must match the actual module
    bytes — a guard that any edit regenerated the manifest. (This is the same
    assertion CI runs; if it fails, run `drift_check --write`.)"""
    drift = dc.verify_integrity(strict=False)
    assert drift == [], (
        "MANIFEST.sha256 is stale vs the package; regenerate with "
        f"`python -m plugins.agent_teams.drift_check --write`. Drift: {drift}"
    )


def test_verify_integrity_strict_raises_on_tamper(tmp_path, monkeypatch):
    """A modified module (hash != manifest) raises under strict mode."""
    # Copy the package into a tmp tree we can tamper with.
    pkg_dir = Path(dc.__file__).resolve().parent
    work = tmp_path / "agent_teams"
    shutil.copytree(pkg_dir, work)
    # Tamper one tracked module.
    (work / "watcher.py").write_text("# tampered\n", encoding="utf-8")

    # Point the module's package-dir + manifest path at the tmp copy.
    monkeypatch.setattr(dc, "_PKG_DIR", work)
    monkeypatch.setattr(dc, "MANIFEST_PATH", work / dc.MANIFEST_NAME)

    with pytest.raises(RuntimeError, match="integrity check FAILED"):
        dc.verify_integrity(strict=True)

    # Non-strict reports the drift list instead of raising.
    drift = dc.verify_integrity(strict=False)
    names = [d[0] for d in drift]
    assert "watcher.py" in names


def test_verify_integrity_no_manifest_is_noop(tmp_path, monkeypatch):
    """A fresh checkout with no manifest yet does not fail (opt-in guard)."""
    work = tmp_path / "empty"
    work.mkdir()
    monkeypatch.setattr(dc, "_PKG_DIR", work)
    monkeypatch.setattr(dc, "MANIFEST_PATH", work / dc.MANIFEST_NAME)
    assert dc.verify_integrity(strict=True) == []


def test_compare_tree_detects_external_divergence(tmp_path):
    """compare_tree() against an install dir whose file diverges from the repo
    manifest reports the drift — the AT-01 cross-copy check (mirrors the live
    VPS-vs-repo diff that started this whole finding)."""
    pkg_dir = Path(dc.__file__).resolve().parent
    install = tmp_path / "installed"
    shutil.copytree(pkg_dir, install)
    # Simulate the VPS hand-edit: append a line to kanban_team.py in the
    # "deployed" copy only.
    target = install / "kanban_team.py"
    target.write_text(target.read_text() + "\n# local hand-edit\n", encoding="utf-8")

    drift = dc.compare_tree(install)  # repo manifest vs the diverged install
    names = [d[0] for d in drift]
    assert "kanban_team.py" in names
    # An untouched module is NOT reported.
    assert "kanban_team_hooks.py" not in names


def test_compare_tree_clean_when_install_matches(tmp_path):
    """A pristine copy of the package matches the manifest (no false drift)."""
    pkg_dir = Path(dc.__file__).resolve().parent
    install = tmp_path / "pristine"
    shutil.copytree(pkg_dir, install)
    assert dc.compare_tree(install) == []


def test_missing_module_is_flagged(tmp_path):
    """A deleted tracked module shows as drift (sentinel), not a silent pass."""
    pkg_dir = Path(dc.__file__).resolve().parent
    install = tmp_path / "incomplete"
    shutil.copytree(pkg_dir, install)
    (install / "watcher.py").unlink()
    drift = dc.compare_tree(install)
    names = {d[0]: d[2] for d in drift}
    assert names.get("watcher.py") == "<missing>"
