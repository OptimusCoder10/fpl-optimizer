"""Synthetic-only admission tests; no pinned archive or held-out loader is opened."""

import ast
import builtins
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from fpl_optimizer.services import heldout_run_guard as guard


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Guard Test", "-c", "user.email=guard@example.invalid",
         "commit", "-m", message)


@pytest.fixture
def setup_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    lock = tmp_path / "external" / "heldout-run-lock.json"
    _git(root, "init", "-q")

    archive_hashes = {}
    for name in guard.PINNED_SHA256:
        payload = f"synthetic,{name}\nNOT_A_REAL_ROW\n".encode()
        (archive / name).write_bytes(payload)
        archive_hashes[name] = _hash(payload)
    monkeypatch.setattr(guard, "PINNED_SHA256", archive_hashes)

    benchmarks = root / "docs" / "benchmarks"
    benchmarks.mkdir(parents=True)
    versions = {"model": "synthetic", "rules": "synthetic", "scoring_adapter": "synthetic"}
    baseline = {"archive_identity": {"sha256": archive_hashes}, "versions": versions}
    baseline_path = benchmarks / "baseline-freeze-2026-10-19.json"
    baseline_path.write_text(json.dumps(baseline))
    baseline_hash = _hash(baseline_path.read_bytes())
    opponent = {"archive_identity": {"sha256": archive_hashes}, "versions": versions,
                "baseline_freeze_sha256": baseline_hash}
    opponent_path = benchmarks / "opponent-freeze-2026-10-20.json"
    opponent_path.write_text(json.dumps(opponent))
    monkeypatch.setattr(guard, "BASELINE_FREEZE_SHA256", baseline_hash)
    monkeypatch.setattr(guard, "OPPONENT_FREEZE_SHA256", _hash(opponent_path.read_bytes()))

    for name in guard.FROZEN_MODULES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# frozen synthetic module\n")
    _commit(root, "synthetic freeze")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(guard, "FREEZE_COMMIT", head)
    return root, archive, lock


def _run(setup_guard, phrase: str = guard.AUTHORISATION_PHRASE):
    root, archive, lock = setup_guard
    return guard.authorize_heldout_run(phrase, archive, repo_root=root, lock_path=lock)


def test_guard_refuses_without_exact_authorisation_phrase(setup_guard):
    for phrase in ("", guard.AUTHORISATION_PHRASE.lower(),
                   guard.AUTHORISATION_PHRASE + " "):
        with pytest.raises(guard.HeldoutGuardError, match="Exact owner authorisation phrase"):
            _run(setup_guard, phrase)
    assert not setup_guard[2].exists()


def test_guard_refuses_when_baseline_freeze_hash_differs(setup_guard):
    root, _, lock = setup_guard
    path = root / "docs/benchmarks/baseline-freeze-2026-10-19.json"
    path.write_text(path.read_text() + " ")
    _commit(root, "change baseline freeze")
    with pytest.raises(guard.HeldoutGuardError, match="Baseline freeze checksum differs"):
        _run(setup_guard)
    assert not lock.exists()


def test_guard_refuses_when_opponent_freeze_hash_differs(setup_guard):
    root, _, lock = setup_guard
    path = root / "docs/benchmarks/opponent-freeze-2026-10-20.json"
    path.write_text(path.read_text() + " ")
    _commit(root, "change opponent freeze")
    with pytest.raises(guard.HeldoutGuardError, match="Opponent freeze checksum differs"):
        _run(setup_guard)
    assert not lock.exists()


def test_guard_refuses_when_git_tree_dirty(setup_guard):
    root, _, lock = setup_guard
    (root / "untracked.txt").write_text("dirty")
    with pytest.raises(guard.HeldoutGuardError, match="Git working tree must be clean"):
        _run(setup_guard)
    assert not lock.exists()


def test_guard_refuses_when_frozen_module_changed_since_freeze(setup_guard):
    root, _, lock = setup_guard
    (root / guard.FROZEN_MODULES[0]).write_text("# changed\n")
    _commit(root, "change frozen module")
    with pytest.raises(guard.HeldoutGuardError, match="frozen module changed"):
        _run(setup_guard)
    assert not lock.exists()


def test_guard_allows_unrelated_new_file_since_freeze(setup_guard):
    root, _, lock = setup_guard
    (root / "future_heldout_runner.py").write_text("# unrelated synthetic file\n")
    _commit(root, "add unrelated future file")
    record = _run(setup_guard)
    assert record["head"] != guard.FREEZE_COMMIT
    assert lock.exists()


def test_guard_refuses_when_archive_checksum_differs(setup_guard):
    _, archive, lock = setup_guard
    (archive / "fixtures.csv").write_bytes(b"altered synthetic bytes")
    with pytest.raises(guard.HeldoutGuardError, match="Pinned archive checksum differs"):
        _run(setup_guard)
    assert not lock.exists()


def test_guard_writes_lock_before_returning_success(setup_guard, monkeypatch):
    root, archive, lock = setup_guard
    monkeypatch.setattr(guard, "__file__", str(root / "backend/src/fpl_optimizer/services/heldout_run_guard.py"))
    record = guard.authorize_heldout_run(guard.AUTHORISATION_PHRASE, archive, lock_path=lock)
    saved = json.loads(setup_guard[2].read_text())
    assert saved == record
    assert saved["head"] == guard.FREEZE_COMMIT
    assert saved["baseline_freeze_sha256"] == guard.BASELINE_FREEZE_SHA256
    assert saved["opponent_freeze_sha256"] == guard.OPPONENT_FREEZE_SHA256
    assert saved["archive_sha256"] == guard.PINNED_SHA256
    assert saved["versions"]["baseline"]["model"] == "synthetic"
    assert saved["timestamp_utc"].endswith("+00:00")


def test_guard_refuses_second_run_when_lock_exists(setup_guard, monkeypatch):
    _run(setup_guard)
    original = setup_guard[2].read_bytes()
    def no_second_hash(_path):
        raise AssertionError("existing lock must refuse before any second-run file reads")
    monkeypatch.setattr(guard, "_sha256", no_second_hash)
    with pytest.raises(guard.HeldoutGuardError, match="owner must move or delete it deliberately"):
        _run(setup_guard)
    assert setup_guard[2].read_bytes() == original


def test_guard_does_not_import_heldout_loader_or_read_archive_rows(setup_guard, monkeypatch):
    archive = setup_guard[1]
    tree = ast.parse(Path(guard.__file__).read_text())
    imported = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names]
    assert not any("retrospective_archive" in name or "heldout_loader" in name
                   or name == "csv" for name in imported if name)
    original_import = builtins.__import__
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_open = Path.open
    archive_paths = {archive / name for name in guard.PINNED_SHA256}
    opened = []

    def checked_import(name, *args, **kwargs):
        if "heldout" in name and name != "fpl_optimizer.services.heldout_run_guard":
            raise AssertionError("held-out loader import")
        return original_import(name, *args, **kwargs)

    def checked_text(path, *args, **kwargs):
        if path in archive_paths:
            raise AssertionError("archive rows decoded")
        return original_read_text(path, *args, **kwargs)

    def checked_bytes(path, *args, **kwargs):
        if path in archive_paths:
            raise AssertionError("archive rows loaded")
        return original_read_bytes(path, *args, **kwargs)

    def checked_open(path, mode="r", *args, **kwargs):
        if path in archive_paths:
            assert mode == "rb"
            opened.append(path.name)
        return original_open(path, mode, *args, **kwargs)

    imports_before = set(sys.modules)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(Path, "read_text", checked_text)
    monkeypatch.setattr(Path, "read_bytes", checked_bytes)
    monkeypatch.setattr(Path, "open", checked_open)
    _run(setup_guard)
    assert not any("heldout" in name for name in set(sys.modules) - imports_before)
    assert set(opened) == set(guard.PINNED_SHA256)
