"""
CI / pre-commit gate for PROTECTED_PATHS.

Fails (exit 1) when the diff being checked touches any protected file
without a matching, human-approved core-safety record in pending_changes/.
This is the technical enforcement of the meta-rule: even if a core-safety
diff is mislabeled 'safe' or approved like an ordinary dangerous change,
this check refuses it until the special approval exists.

Usage:
    python scripts/check_protected_paths.py                # staged changes
    python scripts/check_protected_paths.py <base>..<head> # CI range
"""

import fnmatch
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Kept as a literal copy (not imported) so this gate works even if
# core/self_mod.py itself is the file being tampered with.
PROTECTED_PATHS = (
    "core/self_mod.py",
    "core/review_gate.py",
    "core/prompt.txt",
    "config/api_keys.json",
    "scripts/review_changes.py",
    "scripts/check_protected_paths.py",
    "ui_review.py",
    ".github/workflows/protected-paths.yml",
    "tests/test_self_mod.py",
)


def _changed_files(diff_range: str | None) -> list[str]:
    cmd = ["git", "diff", "--name-only"]
    cmd += [diff_range] if diff_range else ["--cached"]
    r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def _base_ref(diff_range: str | None) -> str:
    return diff_range.split("..")[0] if diff_range else "HEAD"


def _gate_exists_on_base(diff_range: str | None) -> bool:
    """True if the base ref already contains this gate script.

    Bootstrap case: the PR that INTRODUCES the protection regime cannot be
    validated against it — the base has nothing to protect yet. Once merged,
    the base contains the gate and every later touch of a protected file is
    refused without a human core-safety approval.
    """
    r = subprocess.run(
        ["git", "cat-file", "-e", f"{_base_ref(diff_range)}:scripts/check_protected_paths.py"],
        cwd=str(BASE_DIR), capture_output=True, text=True,
    )
    return r.returncode == 0


def _has_core_safety_approval() -> bool:
    """True if any applied/approved core-safety record exists in pending_changes/."""
    pending = BASE_DIR / "pending_changes"
    if not pending.is_dir():
        return False
    for f in pending.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (rec.get("risk_level") == "core_safety_change"
                and rec.get("status") in ("approved", "applied")
                and rec.get("approved_by")):
            return True
    return False


def main() -> int:
    diff_range = sys.argv[1] if len(sys.argv) > 1 else None
    touched = [
        f for f in _changed_files(diff_range)
        if any(fnmatch.fnmatch(f, p) or f == p for p in PROTECTED_PATHS)
    ]
    if not touched:
        print("protected-paths: OK (no protected files touched)")
        return 0

    if not _gate_exists_on_base(diff_range):
        print("protected-paths: BOOTSTRAP — the base ref does not contain this "
              "gate yet, so this diff is the one introducing the protection "
              "regime. Passing; full enforcement starts once it is merged.")
        for f in touched:
            print(f"  - {f}")
        return 0

    if _has_core_safety_approval():
        print("protected-paths: protected files touched, core-safety approval found:")
        for f in touched:
            print(f"  - {f}")
        return 0

    print("protected-paths: REFUSED — diff touches PROTECTED_PATHS without a "
          "human core-safety approval:")
    for f in touched:
        print(f"  - {f}")
    print("\nA human must approve via: python scripts/review_changes.py approve <id>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
