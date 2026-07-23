"""
Human review CLI for pending self-improvement changes.

Terminal counterpart of the HUD review dialogs (ui_review.py). Both
surfaces drive the same protected backend, core/review_gate.py — this
script only adds the interactive confirmation mechanics. It is meant to
be run by a human; the SELF_IMPROVE mode has no tool that invokes it.

Usage:
    python scripts/review_changes.py list
    python scripts/review_changes.py show    <pending-id>
    python scripts/review_changes.py approve <pending-id>
    python scripts/review_changes.py reject  <pending-id> "reason"

Approval flows:
    dangerous          -> show full record, type 'yes' to approve + apply.
    core_safety_change -> separate red "CORE SAFETY CHANGE" screen, forced
                          full-diff walkthrough, a time delay, a typed phrase
                          bound to the diff hash, and a second confirmation.
"""

import getpass
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import review_gate, self_mod  # noqa: E402

RED   = "\033[91m"
BOLD  = "\033[1m"
RESET = "\033[0m"


def cmd_list() -> None:
    records = self_mod.list_pending()
    if not records:
        print("No pending changes.")
        return
    for r in records:
        print(f"{r['id']}  [{r['status']:>8}]  {r['risk_level']:<18} {r['rationale'][:60]}")


def cmd_show(pending_id: str) -> None:
    r = review_gate.get_pending(pending_id)
    print(json.dumps({k: v for k, v in r.items() if k != "diff"}, indent=2))
    print("\n" + review_gate.review_text(r))


def _page_through(text: str) -> None:
    """Print page by page — the reviewer must pass through all of it."""
    lines = text.splitlines() or ["(empty)"]
    for i in range(0, len(lines), 25):
        print("\n".join(lines[i:i + 25]))
        if i + 25 < len(lines):
            input("-- press Enter for more --")


def cmd_approve(pending_id: str) -> None:
    rec = review_gate.get_pending(pending_id)
    if rec["status"] != "pending":
        sys.exit(f"Change is '{rec['status']}', not pending.")

    reviewer = getpass.getuser()
    risk     = review_gate.actual_risk(rec)   # never trust the stored label
    if risk != rec["risk_level"]:
        print(f"{RED}Stored risk '{rec['risk_level']}' != actual '{risk}' — using actual.{RESET}")

    if risk == "core_safety_change":
        _approve_core_safety(rec, reviewer)
    else:
        _page_through(review_gate.review_text(rec))
        answer = input("\nApprove this DANGEROUS change? Type 'yes' to approve: ").strip().lower()
        if answer != "yes":
            print("Not approved.")
            return
        print(review_gate.approve_dangerous(rec["id"], reviewer))


def _approve_core_safety(rec: dict, reviewer: str) -> None:
    print(f"\n{RED}{BOLD}{'=' * 66}")
    print("  CORE SAFETY CHANGE")
    print("  This diff modifies the agent's own safety mechanism.")
    print(f"{'=' * 66}{RESET}")

    shown_at = time.monotonic()
    _page_through(review_gate.review_text(rec))

    # Time delay so this cannot be clicked through reflexively.
    elapsed = time.monotonic() - shown_at
    if elapsed < review_gate.CORE_SAFETY_DELAY_S:
        wait = review_gate.CORE_SAFETY_DELAY_S - elapsed
        print(f"\n(waiting {wait:.0f}s before the approval prompt unlocks...)")
        time.sleep(wait)

    phrase = review_gate.CORE_SAFETY_PHRASE.format(diff_hash=rec["diff_hash"])
    typed  = input(f"\nTo approve, type exactly: {BOLD}{phrase}{RESET}\n> ").strip()
    if typed != phrase:
        print("Phrase mismatch. Not approved.")
        return

    second = input(f"{RED}{review_gate.CORE_SAFETY_WARNING} "
                   f"Type 'I UNDERSTAND' to confirm:{RESET}\n> ").strip()
    if second != "I UNDERSTAND":
        print("Not confirmed. Not approved.")
        return

    print(review_gate.approve_and_apply_core_safety(rec["id"], reviewer))


def cmd_reject(pending_id: str, reason: str) -> None:
    print(review_gate.reject_change(pending_id, reason, getpass.getuser()))


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
