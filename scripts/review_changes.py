"""
Human review CLI for pending self-improvement changes.

This script is the ONLY way to approve DANGEROUS changes and the ONLY way
to approve *and apply* CORE SAFETY changes. It is meant to be run by a
human in a terminal — never by the agent. The SELF_IMPROVE mode has no
tool that invokes it.

Usage:
    python scripts/review_changes.py list
    python scripts/review_changes.py show    <pending-id>
    python scripts/review_changes.py approve <pending-id>
    python scripts/review_changes.py reject  <pending-id> "reason"

Approval flows:
    dangerous          -> show full record, type 'yes' to approve. The agent
                          may then apply it via apply_diff() (hash-matched).
    core_safety_change -> separate red "CORE SAFETY CHANGE" screen, forced
                          full-diff walkthrough, a time delay, a typed phrase
                          bound to the diff hash, and a second confirmation.
                          Applied HERE on its own branch — never by the agent.
"""

import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import self_mod  # noqa: E402

RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

CORE_SAFETY_DELAY_S = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(pending_id: str) -> dict:
    path = self_mod.PENDING_DIR / f"{pending_id}.json"
    if not path.is_file():
        sys.exit(f"No pending change with id: {pending_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(rec: dict) -> None:
    path = self_mod.PENDING_DIR / f"{rec['id']}.json"
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")


def cmd_list() -> None:
    records = self_mod.list_pending()
    if not records:
        print("No pending changes.")
        return
    for r in records:
        print(f"{r['id']}  [{r['status']:>8}]  {r['risk_level']:<18} {r['rationale'][:60]}")


def cmd_show(pending_id: str) -> None:
    r = _load(pending_id)
    print(json.dumps({k: v for k, v in r.items() if k != "diff"}, indent=2))
    print("\n--- DIFF ---")
    print(r["diff"])


def _show_full_record(rec: dict) -> None:
    """Print diff, rationale and rollback plan page by page — reviewer must pass through all of it."""
    sections = [
        ("DIFF", rec["diff"]),
        ("RATIONALE", rec.get("rationale", "(none)")),
        ("EXPECTED EFFECT", rec.get("expected_effect", "(none)")),
        ("ROLLBACK PLAN", rec.get("rollback_plan", "(none)")),
    ]
    for title, body in sections:
        print(f"\n{BOLD}--- {title} ---{RESET}")
        lines = body.splitlines() or ["(empty)"]
        for i in range(0, len(lines), 25):
            print("\n".join(lines[i:i + 25]))
            if i + 25 < len(lines):
                input("-- press Enter for more --")


def _approve_dangerous(rec: dict, reviewer: str) -> None:
    _show_full_record(rec)
    answer = input("\nApprove this DANGEROUS change? Type 'yes' to approve: ").strip().lower()
    if answer != "yes":
        print("Not approved.")
        return
    rec.update(status="approved", approved_by=reviewer, approved_at=_now())
    _save(rec)
    self_mod.log_change({
        "event": "approval", "risk_level": rec["risk_level"],
        "pending_id": rec["id"], "approved_by": reviewer,
        "diff_summary": f"hash={rec['diff_hash']}",
    })
    print("Approved. The agent may now apply it via apply_diff() (content-hash matched).")


def _approve_core_safety(rec: dict, reviewer: str) -> None:
    """Heavier, deliberately slow flow. Applies the change here, on its own branch."""
    print(f"\n{RED}{BOLD}{'=' * 66}")
    print("  CORE SAFETY CHANGE")
    print("  This diff modifies the agent's own safety mechanism.")
    print(f"{'=' * 66}{RESET}")

    shown_at = time.monotonic()
    _show_full_record(rec)

    # Time delay so this cannot be clicked through reflexively.
    elapsed = time.monotonic() - shown_at
    if elapsed < CORE_SAFETY_DELAY_S:
        wait = CORE_SAFETY_DELAY_S - elapsed
        print(f"\n(waiting {wait:.0f}s before the approval prompt unlocks...)")
        time.sleep(wait)

    phrase = f"APPROVE CORE SAFETY {rec['diff_hash']}"
    typed  = input(f"\nTo approve, type exactly: {BOLD}{phrase}{RESET}\n> ").strip()
    if typed != phrase:
        print("Phrase mismatch. Not approved.")
        return

    second = input(
        f"{RED}Really? This change modifies the agent's own safety mechanism. "
        f"Type 'I UNDERSTAND' to confirm:{RESET}\n> "
    ).strip()
    if second != "I UNDERSTAND":
        print("Not confirmed. Not approved.")
        return

    rec.update(status="approved", approved_by=reviewer, approved_at=_now())
    _save(rec)
    self_mod.log_change({
        "event": "approval", "risk_level": "core_safety_change",
        "pending_id": rec["id"], "approved_by": reviewer,
        "diff_summary": f"hash={rec['diff_hash']}",
    })

    # Apply here — on a dedicated branch, never mixed with other changes.
    applied = _apply_core_safety(rec)
    rec.update(status="applied" if applied else "approved",
               applied_at=_now() if applied else None)
    _save(rec)
    print("Applied on a dedicated branch." if applied
          else "Approved but the patch did not apply cleanly — resolve manually.")


def _apply_core_safety(rec: dict) -> bool:
    """Apply an approved core-safety diff on its own branch (human context only)."""
    import subprocess
    base   = self_mod.BASE_DIR
    branch = f"core-safety/{rec['diff_hash']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    patch  = self_mod.PENDING_DIR / f".core-safety-{rec['diff_hash']}.patch"
    diff   = rec["diff"]
    patch.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")
    try:
        def git(*a):
            return subprocess.run(["git", *a], cwd=str(base),
                                  capture_output=True, text=True)
        if git("apply", "--check", "--whitespace=nowarn", str(patch)).returncode != 0:
            return False
        git("checkout", "-b", branch)
        git("apply", "--whitespace=nowarn", str(patch))
        git("add", "-A")
        git("commit", "-m",
            f"core-safety: approved change {rec['diff_hash']}\n\n"
            f"Approved by {rec['approved_by']} via scripts/review_changes.py. "
            f"Rollback: git revert this commit.")
        self_mod.log_change({
            "event": "core_safety_applied", "pending_id": rec["id"],
            "approved_by": rec["approved_by"], "branch": branch,
            "risk_level": "core_safety_change", "applied": True,
        })
        return True
    finally:
        patch.unlink(missing_ok=True)


def cmd_approve(pending_id: str) -> None:
    rec = _load(pending_id)
    if rec["status"] != "pending":
        sys.exit(f"Change is '{rec['status']}', not pending.")

    # Never trust the stored label — re-classify from the diff content.
    actual = self_mod.classify_change(rec["diff"])
    if actual != rec["risk_level"]:
        print(f"{RED}Stored risk '{rec['risk_level']}' != actual '{actual}' — using actual.{RESET}")
        rec["risk_level"] = actual

    reviewer = getpass.getuser()
    if actual == "core_safety_change":
        _approve_core_safety(rec, reviewer)
    else:
        _approve_dangerous(rec, reviewer)


def cmd_reject(pending_id: str, reason: str) -> None:
    rec = _load(pending_id)
    rec.update(status="rejected", rejected_reason=reason,
               rejected_by=getpass.getuser(), rejected_at=_now())
    _save(rec)
    self_mod.log_change({
        "event": "rejection", "pending_id": rec["id"],
        "risk_level": rec["risk_level"], "reason": reason,
        "diff_summary": f"hash={rec['diff_hash']}",
    })
    print("Rejected and logged — the agent will not retry this diff unchanged.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("list", "show", "approve", "reject"):
        sys.exit(__doc__)
    if args[0] == "list":
        cmd_list()
    elif args[0] == "show":
        cmd_show(args[1])
    elif args[0] == "approve":
        cmd_approve(args[1])
    elif args[0] == "reject":
        if len(args) < 3:
            sys.exit("reject requires: <pending-id> \"reason\"")
        cmd_reject(args[1], args[2])


if __name__ == "__main__":
    main()
