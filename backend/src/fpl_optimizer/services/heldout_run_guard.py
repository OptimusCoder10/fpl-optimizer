"""One-time, file-only admission guard for a future held-out comparison.

This module never imports a retrospective loader or parses archive rows. A future
runner must call ``authorize_heldout_run`` before opening any held-out data.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from fpl_optimizer.schemas.prior_archive import PINNED_SHA256


AUTHORISATION_PHRASE = "I authorise the one-time GW20-38 comparison"
BASELINE_FREEZE_SHA256 = "4669732e20e7b686e4d89d4195633db46214a2b9f053e2116c05dcac1d413baa"
OPPONENT_FREEZE_SHA256 = "b6049ea18d1ef87cc737b1befeb1fc2c0adfd61d6710a50d0fcf6ec373015439"
FREEZE_COMMIT = "95ca24f86bae759bb456c64352cb25e122aea0c0"
FROZEN_MODULES = (
    "backend/src/fpl_optimizer/services/prediction_engine.py",
    "backend/src/fpl_optimizer/services/scoring_components.py",
    "backend/src/fpl_optimizer/services/opponent_profiles.py",
    "backend/src/fpl_optimizer/services/opponent_adjustment.py",
    "backend/src/fpl_optimizer/services/retrospective_cohort.py",
    "backend/src/fpl_optimizer/services/retrospective_benchmark.py",
)


class HeldoutGuardError(RuntimeError):
    """Safe refusal without archive values or machine-specific paths."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise HeldoutGuardError("Required frozen file or archive is unavailable.") from exc
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], capture_output=True, text=True,
            check=False,
        )
    except OSError as exc:
        raise HeldoutGuardError("Git verification is unavailable.") from exc
    if result.returncode:
        raise HeldoutGuardError("Git verification failed.")
    return result.stdout.strip()


def authorize_heldout_run(
    authorisation: str,
    archive_dir: Path,
    *,
    repo_root: Path | None = None,
    lock_path: Path | None = None,
) -> dict:
    """Verify frozen inputs, then atomically create the one-time intent record.

    The exact phrase is case-sensitive. The lock is never removed by this guard.
    A future caller may proceed only after this function returns successfully.
    """
    if authorisation != AUTHORISATION_PHRASE:
        raise HeldoutGuardError("Exact owner authorisation phrase is required.")

    root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
    lock = (lock_path or Path.home() / "fpl-benchmark-reports" / "heldout-run-lock.json").resolve()
    if lock.is_relative_to(root):
        raise HeldoutGuardError("The one-time lock must be outside the repository.")
    if lock.exists():
        raise HeldoutGuardError("Held-out run lock already exists; the owner must move or delete it deliberately before a rerun.")

    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HeldoutGuardError("Git working tree must be clean before the held-out run.")
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if len(head) != 40:
        raise HeldoutGuardError("Git HEAD is invalid.")
    if _git(root, "diff", "--name-only", f"{FREEZE_COMMIT}..HEAD", "--", *FROZEN_MODULES):
        raise HeldoutGuardError("A frozen module changed since the opponent freeze commit.")

    benchmarks = root / "docs" / "benchmarks"
    baseline_path = benchmarks / "baseline-freeze-2026-10-19.json"
    opponent_path = benchmarks / "opponent-freeze-2026-10-20.json"
    baseline_hash = _sha256(baseline_path)
    opponent_hash = _sha256(opponent_path)
    if baseline_hash != BASELINE_FREEZE_SHA256:
        raise HeldoutGuardError("Baseline freeze checksum differs from its recorded value.")
    if opponent_hash != OPPONENT_FREEZE_SHA256:
        raise HeldoutGuardError("Opponent freeze checksum differs from its recorded value.")
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        opponent = json.loads(opponent_path.read_text(encoding="utf-8"))
        if opponent["baseline_freeze_sha256"] != baseline_hash:
            raise HeldoutGuardError("Opponent freeze does not reference the verified baseline freeze.")
        if baseline["archive_identity"]["sha256"] != PINNED_SHA256 or opponent["archive_identity"]["sha256"] != PINNED_SHA256:
            raise HeldoutGuardError("Freeze archive checksums differ from the pinned archive identity.")
        for key in ("model", "rules", "scoring_adapter"):
            if baseline["versions"][key] != opponent["versions"][key]:
                raise HeldoutGuardError("Frozen model versions disagree.")
        versions = {"baseline": baseline["versions"], "opponent": opponent["versions"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HeldoutGuardError("Freeze records are invalid.") from exc

    archive_hashes = {name: _sha256(Path(archive_dir) / name) for name in PINNED_SHA256}
    if archive_hashes != PINNED_SHA256:
        raise HeldoutGuardError("Pinned archive checksum differs; held-out run refused.")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all") or _git(root, "rev-parse", "--verify", "HEAD") != head:
        raise HeldoutGuardError("Git state changed during held-out guard verification.")

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "head": head,
        "freeze_commit": FREEZE_COMMIT,
        "baseline_freeze_sha256": baseline_hash,
        "opponent_freeze_sha256": opponent_hash,
        "archive_sha256": archive_hashes,
        "versions": versions,
    }
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise HeldoutGuardError("Held-out run lock already exists; the owner must move or delete it deliberately before a rerun.") from exc
    except OSError as exc:
        raise HeldoutGuardError("Could not create the held-out run lock.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(record, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        raise HeldoutGuardError("Could not complete the held-out run lock; owner must inspect it before retrying.") from exc
    return record
