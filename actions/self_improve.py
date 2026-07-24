"""
SELF_IMPROVE mode for JARVIS / Mark-L.

Entry points:
  - explicit user command (the 'self_improve' tool in main.py),
  - scheduled run (maybe_scheduled_run(), once per SCHEDULE_INTERVAL),
  - a module crossing the RED threshold (auto_trigger_check()).

Each run analyzes module statuses + the audit log, asks the LLM for small
fixes, applies SAFE diffs automatically (git branch + tests + auto-revert)
and queues DANGEROUS / CORE-SAFETY diffs for human review. Hard limits keep
the loop bounded — see RUN LIMITS below.
"""

import ast
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from core import self_mod

# ── RUN LIMITS (bounded loop, no runaway self-modification) ──────────────────
MAX_ITERATIONS        = 10    # LLM proposal rounds per run
MAX_AUTO_FILES        = 5     # files auto-modified before stopping to summarize
TIME_BUDGET_S         = 600   # wall-clock budget per run
STAGNATION_LIMIT      = 3     # attempts on one module without status change -> give up
SCHEDULE_INTERVAL_S   = 24 * 3600   # scheduled run at most once a day

STATE_PATH = self_mod.BASE_DIR / "memory" / "self_improve_state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _rejected_hashes() -> set[str]:
    """Diff hashes a human already rejected — never re-propose unchanged."""
    return {r["diff_hash"] for r in self_mod.list_pending() if r.get("status") == "rejected"}


def _pick_targets(overview: dict, explicit: str | None) -> list[str]:
    """Modules to work on: explicit target, else RED first, then YELLOW with errors."""
    modules = overview["modules"]
    if explicit:
        return [m for m in modules if explicit in m]
    red    = [m for m, e in modules.items() if e["status"] == "RED"]
    yellow = [m for m, e in modules.items() if e["status"] == "YELLOW" and e.get("errors", 0) > 0]
    return red + yellow


def run_self_improve(target: str | None = None,
                     instruction: str | None = None,
                     model=None,
                     log=print) -> str:
    """One bounded SELF_IMPROVE run. Returns a human-readable summary."""
    started = time.monotonic()
    overview = self_mod.analyze_codebase()
    targets  = _pick_targets(overview, target)
    rejected = _rejected_hashes()

    if not targets and not instruction:
        return "SELF_IMPROVE: no RED or error-prone modules found — nothing to do."

    applied, queued, skipped = [], [], []
    stagnation: dict[str, int] = {}
    iterations = 0

    work = targets or ["(general)"]
    for module in work:
        if iterations >= MAX_ITERATIONS:
            log("[SelfImprove] Iteration limit reached — stopping.")
            break
        if len(applied) >= MAX_AUTO_FILES:
            log("[SelfImprove] Auto-file limit reached — stopping to summarize.")
            break
        if time.monotonic() - started > TIME_BUDGET_S:
            log("[SelfImprove] Time budget exhausted — stopping.")
            break
        if stagnation.get(module, 0) >= STAGNATION_LIMIT:
            skipped.append(f"{module} (no status change after {STAGNATION_LIMIT} attempts)")
            continue

        iterations += 1
        status_before = overview["modules"].get(module, {}).get("status", "YELLOW")
        problem = instruction or _describe_problem(module, overview)
        code    = _relevant_code(module)

        log(f"[SelfImprove] ({iterations}/{MAX_ITERATIONS}) {module}: proposing fix...")
        try:
            proposal = self_mod.propose_improvement(problem, code, model=model)
        except Exception as e:
            skipped.append(f"{module} (proposal failed: {str(e)[:80]})")
            continue

        h = self_mod.diff_hash(proposal["diff"])
        if h in rejected:
            skipped.append(f"{module} (diff {h} was previously rejected — not retrying)")
            continue

        risk = proposal["risk_level"]
        if risk == "safe":
            ok = self_mod.apply_diff(proposal["diff"], risk)
            if ok:
                applied.append(f"{module} ({h})")
            else:
                stagnation[module] = stagnation.get(module, 0) + 1
                skipped.append(f"{module} (safe diff failed to apply or regressed)")
        else:
            pending_id = self_mod.queue_pending_change(proposal)
            queued.append(f"{module} -> {pending_id} [{risk}]")

        status_after = self_mod._load_status().get(module, {}).get("status", status_before)
        self_mod.log_change({
            "module": module,
            "event": "self_improve_iteration",
            "problem_description": problem[:200],
            "diff_summary": proposal.get("rationale", "")[:200],
            "risk_level": risk,
            "applied": risk == "safe" and bool(applied and applied[-1].startswith(module)),
            "approved_by": None,
            "status_before": status_before,
            "status_after": status_after,
        })
        if status_after == status_before and risk == "safe":
            stagnation[module] = stagnation.get(module, 0) + 1

    state = _load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_state(state)

    lines = [f"SELF_IMPROVE finished ({iterations} iteration(s))."]
    if applied:
        lines.append(f"Applied automatically (SAFE): {', '.join(applied)}")
    if queued:
        lines.append(f"Waiting for human review: {', '.join(queued)} "
                     f"— review with: python scripts/review_changes.py list")
    if skipped:
        lines.append(f"Skipped: {'; '.join(skipped)}")
    if not (applied or queued):
        lines.append("No changes were made.")
    return "\n".join(lines)


def _describe_problem(module: str, overview: dict) -> str:
    entry  = overview["modules"].get(module, {})
    audit  = [e for e in self_mod.read_audit_log(100) if e.get("module") == module]
    status = self_mod._load_status().get(module, {})
    return (
        f"Module {module} has status {entry.get('status')} "
        f"({entry.get('reason', 'no reason recorded')}). "
        f"Last recorded error: {status.get('last_error', 'none')}. "
        f"Recent audit events: {json.dumps(audit[-3:], ensure_ascii=False)[:800]}. "
        f"Propose ONE small, focused fix that addresses the recorded errors "
        f"or adds missing error handling. Do not refactor unrelated code."
    )


def _relevant_code(module: str) -> str:
    if module == "(general)":
        return ""
    try:
        return self_mod.read_file(module)
    except Exception:
        return ""


# ── Scheduled / threshold triggers ───────────────────────────────────────────

def maybe_scheduled_run(model=None) -> str | None:
    """Run at most once per SCHEDULE_INTERVAL_S; None when it's not time yet."""
    state = _load_state()
    last  = state.get("last_run")
    if last:
        try:
            last_ts = datetime.fromisoformat(last).timestamp()
            if time.time() - last_ts < SCHEDULE_INTERVAL_S:
                return None
        except ValueError:
            pass
    return run_self_improve(model=model)


def auto_trigger_check(model=None) -> str | None:
    """Trigger a run if any module is RED; None otherwise."""
    overview = self_mod.analyze_codebase()
    red = [m for m, e in overview["modules"].items() if e["status"] == "RED"]
    if not red:
        return None
    return run_self_improve(target=None, model=model)


def add_skill(name: str, description: str, code: str, model=None) -> str:
    """Create a brand-new action module and register it as a tool.

    The new file goes through the SAME safety pipeline as any other change
    (classify_change, git branch, tests before/after, quality check,
    auto-revert on regression) — creating a file that doesn't touch a
    protected/dangerous path or dangerous content classifies as SAFE and
    applies immediately; anything else is queued for human review like any
    other diff. Only on a successful SAFE apply is the action wired into
    the tool registry, and it starts YELLOW — it must earn GREEN through
    error-free calls, same as if a human had added it by hand.

    Convention: `code` must define `run(parameters: dict, **kwargs)`.
    """
    from actions import registry

    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return f"Invalid skill name '{name}' — use lowercase snake_case."
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"Skill code does not parse: {e}"
    if "def run(" not in code:
        return "Skill code must define a function named run(parameters, **kwargs)."

    path = f"actions/{name}.py"
    if (self_mod.BASE_DIR / path).exists():
        return f"'{path}' already exists — choose a different name."

    diff = self_mod.make_create_file_diff(path, code)
    risk = self_mod.classify_change(diff)
    if risk != "safe":
        pending_id = self_mod.queue_pending_change({
            "diff": diff, "risk_level": risk,
            "rationale": f"New skill '{name}': {description}",
            "expected_effect": f"Adds a '{name}' tool once approved.",
            "rollback_plan": "git revert the applying commit.",
        })
        return (f"New skill '{name}' classified as {risk} — queued for your "
                f"review: {pending_id}")

    if not self_mod.apply_diff(diff, "safe"):
        return f"Could not create '{path}' — the file failed to apply cleanly or regressed tests."

    declaration = {
        "name": name, "description": description,
        "parameters": {"type": "OBJECT", "properties": {}},
    }
    return registry.register_action(name, f"actions.{name}", "run", declaration)


# ── Tool entry point (called from main.py) ───────────────────────────────────

def self_improve(parameters: dict, player=None, speak=None, **_) -> str:
    """Voice/UI tool: action = run | status | pending | audit | add_skill."""
    p      = parameters or {}
    action = (p.get("action") or "status").strip().lower()

    def log(msg: str):
        print(msg)
        if player:
            player.write_log(msg)

    if action == "run":
        return run_self_improve(
            target=p.get("target") or None,
            instruction=p.get("instruction") or None,
            log=log,
        )

    if action == "add_skill":
        name        = (p.get("name") or "").strip().lower()
        description = (p.get("description") or "").strip()
        code        = p.get("code") or ""
        if not (name and description and code):
            return "add_skill needs 'name', 'description', and 'code'."
        return add_skill(name, description, code)

    if action == "pending":
        records = self_mod.list_pending()
        waiting = [r for r in records if r["status"] == "pending"]
        if not waiting:
            return "No changes are waiting for approval."
        return "Waiting for your approval:\n" + "\n".join(
            f"- {r['id']} [{r['risk_level']}]: {r['rationale'][:70]}" for r in waiting
        ) + "\nApprove or reject with: python scripts/review_changes.py"

    if action == "audit":
        entries = self_mod.read_audit_log(10)
        if not entries:
            return "Audit log is empty."
        return "Last self-improvement events:\n" + "\n".join(
            f"- {e['timestamp']} {e.get('event', '?')} {e.get('module', '')}" for e in entries
        )

    # default: status overview
    overview = self_mod.analyze_codebase()
    counts = {"RED": 0, "YELLOW": 0, "GREEN": 0}
    reds = []
    for m, e in overview["modules"].items():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
        if e["status"] == "RED":
            reds.append(f"{m} ({e['reason']})")
    msg = (f"Module health: {counts['GREEN']} green, "
           f"{counts['YELLOW']} yellow, {counts['RED']} red.")
    if reds:
        msg += " RED: " + "; ".join(reds)
    return msg
