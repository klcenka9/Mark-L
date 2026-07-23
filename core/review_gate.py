"""
Shared approval gate for pending self-improvement changes.

Backend used by BOTH human review surfaces — the terminal CLI
(scripts/review_changes.py) and the PyQt dialogs (ui_review.py). It owns
the state transitions pending -> approved/rejected -> applied, always
re-classifies the diff from content (stored labels are never trusted),
and writes every decision to the audit log.

This file is in PROTECTED_PATHS: the agent's own pipeline can never
modify it, and diffs that merely *call* into it classify as dangerous
(see DANGEROUS_CONTENT_PATTERNS in core/self_mod.py).
"""

import json
import subprocess
from datetime import datetime, timezone

from core import self_mod

# UI/CLI mechanics shared by both surfaces
CORE_SAFETY_DELAY_S = 5
CORE_SAFETY_PHRASE  = "APPROVE CORE SAFETY {diff_hash}"
CORE_SAFETY_WARNING = ("Really? This change modifies the safety mechanism "
                       "of the agent itself.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_pending(pending_id: str) -> dict:
    """Load one pending record by id."""
    path = self_mod.PENDING_DIR / f"{pending_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No pending change with id: {pending_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(rec: dict) -> None:
    (self_mod.PENDING_DIR / f"{rec['id']}.json").write_text(
        json.dumps(rec, indent=2), encoding="utf-8")


def actual_risk(rec: dict) -> str:
    """Binding risk level, re-derived from the diff content."""
    return self_mod.classify_change(rec["diff"])


def review_text(rec: dict) -> str:
    """Full reviewable text: diff, rationale, expected effect, rollback plan."""
    return "\n".join([
        "--- DIFF " + "-" * 50, rec["diff"],
        "", "--- RATIONALE " + "-" * 45, rec.get("rationale", "(none)"),
        "", "--- EXPECTED EFFECT " + "-" * 39, rec.get("expected_effect", "(none)"),
        "", "--- ROLLBACK PLAN " + "-" * 41, rec.get("rollback_plan", "(none)"),
    ])


def approve_dangerous(pending_id: str, approved_by: str) -> str:
    """Approve an ordinary DANGEROUS change and apply it (hash-matched).

    Refuses records whose content actually classifies as core_safety_change —
    those must go through approve_and_apply_core_safety's heavier flow.
    """
    rec = get_pending(pending_id)
    if rec["status"] != "pending":
        return f"Change is '{rec['status']}', not pending."

    risk = actual_risk(rec)
    if risk == "core_safety_change":
        raise PermissionError(
            "This diff touches PROTECTED_PATHS — it requires the separate "
            "CORE SAFETY approval flow, not the ordinary dangerous one.")

    rec.update(status="approved", approved_by=approved_by,
               approved_at=_now(), risk_level=risk)
    _save(rec)
    self_mod.log_change({
        "event": "approval", "risk_level": risk, "pending_id": rec["id"],
        "approved_by": approved_by, "diff_summary": f"hash={rec['diff_hash']}",
    })

    applied = self_mod.apply_diff(rec["diff"], risk)
    if applied:
        rec.update(status="applied", applied_at=_now())
        _save(rec)
        return "Approved and applied on a new branch."
    return "Approved, but the patch did not apply cleanly — resolve manually."


def approve_and_apply_core_safety(pending_id: str, approved_by: str) -> str:
    """Approve + apply a CORE SAFETY change on its own dedicated branch.

    Only the human review surfaces call this, after their own confirmation
    mechanics (CLI: typed hash-bound phrase; UI: scroll-through + time delay
    + double confirmation). The approval is audit-logged like any other.
    """
    rec = get_pending(pending_id)
    if rec["status"] != "pending":
        return f"Change is '{rec['status']}', not pending."

    rec["risk_level"] = actual_risk(rec)
    rec.update(status="approved", approved_by=approved_by, approved_at=_now())
    _save(rec)
    self_mod.log_change({
        "event": "approval", "risk_level": "core_safety_change",
        "pending_id": rec["id"], "approved_by": approved_by,
        "diff_summary": f"hash={rec['diff_hash']}",
    })

    applied = _apply_on_core_safety_branch(rec)
    rec.update(status="applied" if applied else "approved",
               applied_at=_now() if applied else None)
    _save(rec)
    return ("Applied on a dedicated core-safety branch." if applied
            else "Approved, but the patch did not apply cleanly — resolve manually.")


def _apply_on_core_safety_branch(rec: dict) -> bool:
    """Apply an approved core-safety diff on its own branch, never mixed."""
    base   = self_mod.BASE_DIR
    branch = f"core-safety/{rec['diff_hash']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    patch  = self_mod.PENDING_DIR / f".core-safety-{rec['diff_hash']}.patch"
    diff   = rec["diff"]
    patch.parent.mkdir(parents=True, exist_ok=True)
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
            f"Approved by {rec['approved_by']}. Rollback: git revert this commit.")
        self_mod.log_change({
            "event": "core_safety_applied", "pending_id": rec["id"],
            "approved_by": rec["approved_by"], "branch": branch,
            "risk_level": "core_safety_change", "applied": True,
        })
        return True
    finally:
        patch.unlink(missing_ok=True)


def reject_change(pending_id: str, reason: str, rejected_by: str) -> str:
    """Reject a pending change; logged so the agent never retries it unchanged."""
    rec = get_pending(pending_id)
    rec.update(status="rejected", rejected_reason=reason,
               rejected_by=rejected_by, rejected_at=_now())
    _save(rec)
    self_mod.log_change({
        "event": "rejection", "pending_id": rec["id"],
        "risk_level": rec["risk_level"], "reason": reason,
        "diff_summary": f"hash={rec['diff_hash']}",
    })
    return "Rejected and logged — the agent will not retry this diff unchanged."
