"""
Self-modification engine for JARVIS / Mark-L.

This module lets the assistant read, analyze and (safely) modify its own
codebase. Every write goes through a classification + approval gate:

    safe                -> applied automatically on a dedicated git branch
    dangerous           -> queued in pending_changes/, waits for human approval
    core_safety_change  -> NEVER applied by this module. Only the human-run
                           CLI (scripts/review_changes.py) can apply it, with
                           its own heavier confirmation flow.

META-RULE: any change touching PROTECTED_PATHS is core_safety_change,
without exception. apply_diff() refuses those diffs entirely — the agent
cannot widen its own permissions or lower its own safety bar.
"""

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR          = get_base_dir()
PENDING_DIR       = BASE_DIR / "pending_changes"
AUDIT_LOG_PATH    = BASE_DIR / "logs" / "self_improve_audit.jsonl"
STATUS_PATH       = BASE_DIR / "memory" / "module_status.json"

# ── PROTECTED_PATHS ──────────────────────────────────────────────────────────
# Files that form the safety core. A diff touching ANY of these is always
# classified core_safety_change and can never be applied by apply_diff().
# This constant is itself inside a protected file, so the agent cannot
# shrink the list through its own pipeline.
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
    # The orchestrator's own run limits (MAX_ITERATIONS, MAX_AUTO_FILES,
    # TIME_BUDGET_S, STAGNATION_LIMIT) and the dynamic-skill registration
    # mechanism ARE the safety mechanism in spirit, even though they live
    # outside core/. A "safe"-classified diff to any of these could quietly
    # raise its own limits or weaken its own validation — the exact
    # self-widening the meta-rule exists to prevent.
    "actions/self_improve.py",
    "actions/registry.py",
    # main.py wires tool dispatch, the self_improve scheduler, and the
    # record_call() hooks that feed the status system; it also has zero
    # smoke-test coverage (root-level file, excluded from _import_smoke).
    "main.py",
    # The only test that currently pins the run-limit constants and the
    # registry's name/module validation — must not be weakened alongside
    # the code it guards.
    "tests/test_smoke_imports.py",
)

# ── Risk classification rules ────────────────────────────────────────────────
# Paths whose modification is always at least DANGEROUS (system actions,
# credentials, deployment, user-file manipulation).
DANGEROUS_PATH_PATTERNS = (
    "config/*",
    "actions/computer_settings.py",   # power / network / OS actions
    "actions/computer_control.py",
    "actions/file_controller.py",     # moves/deletes user files
    "actions/send_message.py",        # sends messages on user's behalf
    "dashboard/server.py",            # network-exposed surface
    "core/installer.py",
    "setup.py",
    "requirements.txt",               # dependency/supply-chain changes
    ".github/*",
)

# Added lines matching any of these make a diff at least DANGEROUS,
# regardless of which file it touches.
DANGEROUS_CONTENT_PATTERNS = (
    r"\bos\.remove\b", r"\bos\.unlink\b", r"\bshutil\.rmtree\b", r"\bos\.rmdir\b",
    r"\brm\s+-rf\b", r"\bdel\s+/[sq]\b", r"\bformat\s+[a-z]:",
    r"\bshutdown\b", r"\bnetsh\b", r"\bfirewall\b", r"\bwinreg\b", r"\breg\s+add\b",
    r"api[_-]?key", r"\btoken\b", r"\bpassword\b", r"\bsecret\b",
    r"\bdeploy\b", r"\bproduction\b",
    r"\beval\s*\(", r"\bexec\s*\(",
    # Code that reaches into the safety API is never auto-applied:
    r"\breview_gate\b", r"\bapprove_and_apply\b", r"\bapprove_dangerous\b",
    r"\bapply_diff\s*\(", r"\bclassify_change\s*\(", r"\bPROTECTED_PATHS\b",
)

# Status thresholds — measurable, not subjective.
RED_ERROR_RATE    = 0.20   # >20 % errors over the tracked window -> RED
STATUS_WINDOW     = 20     # error rate computed over last N calls
MIN_CALLS_FOR_RED = 5      # don't go RED on tiny samples

RISK_ORDER = {"safe": 0, "dangerous": 1, "core_safety_change": 2}


# ── Small helpers ────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args: str, base_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command in the project root and capture output."""
    return subprocess.run(
        ["git", *args],
        cwd=str(base_dir or BASE_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def diff_hash(diff: str) -> str:
    """Stable short identifier of a diff (used to bind approvals to content)."""
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]


def _diff_paths(diff: str) -> list[str]:
    """Extract every file path a unified diff touches."""
    paths: set[str] = set()
    for line in diff.splitlines():
        m = re.match(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)", line)
        if m and m.group(1) != "/dev/null":
            paths.add(m.group(1))
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            paths.update(m.groups())
    return sorted(paths)


def _added_lines(diff: str) -> list[str]:
    return [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]


def _safe_project_path(path: str, base_dir: Path | None = None) -> Path:
    """Resolve a path and refuse anything outside the project root."""
    base = (base_dir or BASE_DIR).resolve()
    full = (base / path).resolve()
    if base != full and base not in full.parents:
        raise ValueError(f"Path escapes project root: {path}")
    return full


# ── Read-only introspection ──────────────────────────────────────────────────

def list_files(directory: str = ".") -> list[str]:
    """Return project files under `directory`, respecting .gitignore."""
    _safe_project_path(directory)
    r = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", directory)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {r.stderr.strip()}")
    return sorted(l for l in r.stdout.splitlines() if l.strip())


def read_file(path: str) -> str:
    """Safely read a file from the project's own codebase."""
    full = _safe_project_path(path)
    if not full.is_file():
        raise FileNotFoundError(path)
    return full.read_text(encoding="utf-8", errors="replace")


def analyze_codebase() -> dict:
    """Overview of main modules (actions/core/memory/ui/config) with RED/YELLOW/GREEN status."""
    overview: dict = {"generated": _now(), "modules": {}}
    statuses = _load_status()
    for path in list_files("."):
        if not path.endswith(".py"):
            continue
        group = path.split("/")[0] if "/" in path else "root"
        entry = statuses.get(path, {})
        overview["modules"][path] = {
            "group":  group,
            "status": entry.get("status", "YELLOW"),   # unknown = not yet earned GREEN
            "calls":  entry.get("calls", 0),
            "errors": entry.get("errors", 0),
            "reason": entry.get("reason", "no data yet"),
        }
    return overview


# ── Status system (RED / YELLOW / GREEN) ─────────────────────────────────────

def _load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_status(data: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_status(module: str, status: str, reason: str) -> None:
    """Persist a module's status; every transition is audit-logged."""
    if status not in ("RED", "YELLOW", "GREEN"):
        raise ValueError(f"Invalid status: {status}")
    data  = _load_status()
    entry = data.setdefault(module, {})
    old   = entry.get("status")
    entry.update({"status": status, "reason": reason, "updated": _now()})
    _save_status(data)
    if old != status:
        log_change({
            "module": module, "event": "status_change",
            "status_before": old, "status_after": status, "reason": reason,
        })


def record_call(module: str, ok: bool, error: str = "") -> None:
    """Record one runtime call result and recompute the module's status."""
    data  = _load_status()
    entry = data.setdefault(module, {"status": "YELLOW", "reason": "new module"})
    window = entry.setdefault("window", [])
    window.append(0 if ok else 1)
    del window[:-STATUS_WINDOW]
    entry["calls"]  = entry.get("calls", 0) + 1
    entry["errors"] = entry.get("errors", 0) + (0 if ok else 1)
    if not ok:
        entry["last_error"] = error[:300]

    rate = sum(window) / len(window)
    if len(window) >= MIN_CALLS_FOR_RED and rate > RED_ERROR_RATE:
        new, why = "RED", f"error rate {rate:.0%} over last {len(window)} calls"
    elif rate > 0:
        new, why = "YELLOW", f"error rate {rate:.0%} over last {len(window)} calls"
    else:
        new, why = "GREEN", f"0 errors over last {len(window)} calls"

    old = entry.get("status")
    entry.update({"status": new, "reason": why, "updated": _now()})
    _save_status(data)
    if old != new:
        log_change({
            "module": module, "event": "status_change",
            "status_before": old, "status_after": new, "reason": why,
        })


# ── Classification ───────────────────────────────────────────────────────────

def _is_protected(path: str) -> bool:
    """True if `path` matches an entry in PROTECTED_PATHS."""
    return any(fnmatch.fnmatch(path, pat) or path == pat for pat in PROTECTED_PATHS)


def classify_change(diff: str) -> str:
    """Classify a diff as 'safe', 'dangerous' or 'core_safety_change'.

    META-RULE: any touch of PROTECTED_PATHS => core_safety_change, always.
    SELF_IMPROVE mode must never modify this function or the approval gate;
    such diffs are themselves core_safety_change by the same rule.
    """
    paths = _diff_paths(diff)
    if not paths:
        raise ValueError("Diff touches no recognizable files.")

    if any(_is_protected(p) for p in paths):
        return "core_safety_change"

    for p in paths:
        if any(fnmatch.fnmatch(p, pat) or p == pat for pat in DANGEROUS_PATH_PATTERNS):
            return "dangerous"

    added = "\n".join(_added_lines(diff))
    for pattern in DANGEROUS_CONTENT_PATTERNS:
        if re.search(pattern, added, re.IGNORECASE):
            return "dangerous"

    return "safe"


# ── Proposals ────────────────────────────────────────────────────────────────

def propose_improvement(problem_description: str, relevant_code: str, model=None) -> dict:
    """Ask the LLM for a concrete improvement; returns a structured proposal.

    The model's own risk opinion is recorded, but the binding risk_level
    always comes from classify_change() — the model cannot downgrade risk.
    """
    if model is None:
        model = _default_model()

    prompt = f"""You are a senior Python engineer improving the JARVIS assistant's own codebase.

Problem:
{problem_description}

Relevant code:
{relevant_code[:8000]}

Return your answer as exactly TWO fenced blocks and nothing else.

First, a metadata block (the diff does NOT go here):
```json
{{
  "rationale": "why this change",
  "expected_effect": "what should improve",
  "risk_level": "safe | dangerous",
  "rollback_plan": "how to revert (git revert of the created commit)",
  "tests_to_run": ["tests/test_smoke_imports.py"]
}}
```

Then the change itself as a unified diff, in its own block:
```diff
--- a/path/to/file.py
+++ b/path/to/file.py
@@ ... @@
 <context and +/- lines>
```

Rules:
- Small, reviewable diff. No refactors beyond the problem.
- Put the diff ONLY in the ```diff block — never inside the JSON.
- Never touch: {', '.join(PROTECTED_PATHS)}
- No irreversible operations (deletes, credential changes)."""

    response = model.generate_content(prompt)
    proposal = _parse_proposal(response.text or "")

    proposal["model_risk_opinion"] = proposal.get("risk_level")
    proposal["risk_level"] = classify_change(proposal["diff"])   # binding
    proposal.setdefault("tests_to_run", [])
    return proposal


def _extract_fence(text: str, lang: str) -> str | None:
    """Return the body of the first ```<lang> ... ``` block, or None."""
    m = re.search(rf"```{lang}\b[^\n]*\r?\n(.*?)\r?\n?```", text, re.DOTALL)
    return m.group(1) if m else None


def _parse_proposal(text: str) -> dict:
    """Extract a proposal from raw model output.

    Prefers separate ```diff and ```json fenced blocks — robust because the
    diff (full of quotes and newlines) never has to survive JSON escaping.
    Falls back to a single JSON object with an embedded diff for models that
    still answer that way.
    """
    diff = _extract_fence(text, "diff")
    meta = _extract_fence(text, "json")

    proposal: dict = {}
    if meta:
        try:
            proposal = json.loads(meta)
        except json.JSONDecodeError:
            proposal = {}
    if diff is not None:
        proposal["diff"] = diff

    if not proposal.get("diff"):
        # Fallback: whole output is one JSON object with the diff inside it.
        raw = re.sub(r"^```[a-zA-Z]*\r?\n?|\r?\n?```\s*$", "", text.strip())
        proposal = json.loads(raw)

    for key in ("diff", "rationale", "expected_effect", "rollback_plan"):
        proposal.setdefault(key, "")
    if not str(proposal["diff"]).strip():
        raise ValueError("Model returned no usable diff.")
    return proposal


def _default_model():
    """Gemini client, same pattern as actions/dev_agent.py."""
    from google import genai
    api_key = json.loads(
        (BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")
    )["gemini_api_key"]
    client = genai.Client(api_key=api_key)

    class _W:
        def generate_content(self, contents):
            return client.models.generate_content(
                model="gemini-2.5-flash", contents=contents)

    return _W()


# ── Pending changes & approvals ──────────────────────────────────────────────

def queue_pending_change(proposal: dict) -> str:
    """Write a dangerous/core-safety proposal to pending_changes/ for human review."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    h  = diff_hash(proposal["diff"])
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    record = {
        "id": f"{ts}-{h}",
        "created": _now(),
        "diff_hash": h,
        "risk_level": proposal["risk_level"],
        "diff": proposal["diff"],
        "rationale": proposal.get("rationale", ""),
        "expected_effect": proposal.get("expected_effect", ""),
        "rollback_plan": proposal.get("rollback_plan", ""),
        "tests_to_run": proposal.get("tests_to_run", []),
        "status": "pending",          # pending | approved | rejected | applied
        "approved_by": None,
        "rejected_reason": None,
    }
    path = PENDING_DIR / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    log_change({
        "module": ", ".join(_diff_paths(proposal["diff"])),
        "event": "queued_for_review",
        "risk_level": record["risk_level"],
        "diff_summary": record["rationale"][:200],
        "pending_id": record["id"],
        "applied": False,
    })
    return record["id"]


def list_pending() -> list[dict]:
    """All pending-change records, oldest first."""
    if not PENDING_DIR.is_dir():
        return []
    records = []
    for f in sorted(PENDING_DIR.glob("*.json")):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return records


def _find_approval(h: str) -> dict | None:
    """Return the approved pending record matching this diff hash, if any."""
    for rec in list_pending():
        if rec.get("diff_hash") == h and rec.get("status") == "approved":
            return rec
    return None


def _mark_pending(pending_id: str, **fields) -> None:
    path = PENDING_DIR / f"{pending_id}.json"
    rec  = json.loads(path.read_text(encoding="utf-8"))
    rec.update(fields)
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")


# ── Applying diffs ───────────────────────────────────────────────────────────

def apply_diff(diff: str, risk_level: str) -> bool:
    """Apply a diff through the safety gate.

    - core_safety_change: ALWAYS refused here. Only scripts/review_changes.py
      (run by a human, outside the agent's tool surface) can apply those.
    - dangerous: applied only if a human already approved this exact diff
      (matched by content hash); otherwise it is queued and False is returned.
    - safe: applied automatically on a new git branch, tests before/after,
      automatic git revert on regression.

    The caller's risk_level is advisory only — the diff is re-classified
    here and the stricter of the two wins.
    """
    actual = classify_change(diff)
    if RISK_ORDER[actual] > RISK_ORDER.get(risk_level, 0):
        risk_level = actual

    if risk_level == "core_safety_change":
        log_change({
            "module": ", ".join(_diff_paths(diff)),
            "event": "refused_core_safety",
            "risk_level": risk_level, "applied": False,
            "diff_summary": f"hash={diff_hash(diff)}",
        })
        raise PermissionError(
            "core_safety_change: this diff touches PROTECTED_PATHS and cannot "
            "be applied by the agent. A human must run scripts/review_changes.py."
        )

    if risk_level == "dangerous":
        approval = _find_approval(diff_hash(diff))
        if approval is None:
            queue_pending_change({
                "diff": diff, "risk_level": "dangerous",
                "rationale": "queued by apply_diff (no prior approval)",
            })
            return False
        applied = _apply_on_branch(diff, risk_level, approved_by=approval["approved_by"])
        if applied:
            _mark_pending(approval["id"], status="applied", applied_at=_now())
        return applied

    return _apply_on_branch(diff, risk_level, approved_by=None)


# Progressively more tolerant `git apply` strategies. Model-generated diffs
# commonly (a) have LF endings that must patch this repo's CRLF files — a CR
# is trailing whitespace to git, handled by --ignore-whitespace — and
# (b) carry wrong @@ line counts, which --recount recomputes. The last
# strategy tolerates both at once. Shared by the SAFE path (below) and the
# core-safety path (core/review_gate.py) so both get the same tolerance.
APPLY_STRATEGIES = (
    ["--whitespace=nowarn"],
    ["--whitespace=nowarn", "--ignore-whitespace"],
    ["--whitespace=nowarn", "--recount"],
    ["--whitespace=nowarn", "--recount", "--ignore-whitespace"],
)


def dirty_tracked_files(base_dir: Path | None = None) -> list[str]:
    """`git status --porcelain` lines for TRACKED files only (excludes `??`
    untracked entries). Non-empty means something other than the diff about
    to be applied is uncommitted — used to refuse an apply that would
    otherwise sweep unrelated local edits into a self-improve commit."""
    return [l for l in _git("status", "--porcelain", base_dir=base_dir).stdout.splitlines()
            if l and not l.startswith("??")]


def apply_patch_with_fallback(patch_file: Path, cwd: Path | None = None) -> list[str] | None:
    """Return the first APPLY_STRATEGIES flag set that applies `patch_file`
    cleanly (dry-run via --check), or None if none of them do."""
    for flags in APPLY_STRATEGIES:
        if _git("apply", "--check", *flags, str(patch_file), base_dir=cwd).returncode == 0:
            return flags
    return None


def protected_content_hash(paths: list[str], base_dir: Path | None = None) -> str:
    """Deterministic hash of the CURRENT on-disk content of `paths`.

    Binds a core-safety approval to the exact end-state of the files it
    covers, not to diff text — so it is immune to how the diff was expressed
    (whitespace, --recount adjustments, model formatting quirks) and to
    reuse against any *other* diff, since any different content produces a
    different hash. Any file not present is hashed as "<deleted>", so a
    diff that removes a protected file still requires a fresh approval.
    """
    base = base_dir or BASE_DIR
    h = hashlib.sha256()
    for p in sorted(paths):
        full = base / p
        content = full.read_bytes() if full.is_file() else b"<deleted>"
        h.update(p.encode("utf-8") + b"\0" + content + b"\0")
    return h.hexdigest()[:16]


def make_create_file_diff(path: str, content: str) -> str:
    """Build a unified diff that creates a brand-new file at `path`."""
    lines = content.splitlines()
    body = "\n".join(f"+{l}" for l in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def _apply_on_branch(diff: str, risk_level: str, approved_by: str | None) -> bool:
    """git branch -> apply -> commit -> tests; revert and report on regression.

    On success, fast-forward-merges the self-improve branch back into the
    branch this call started on — SAFE changes are pre-vetted (classified
    safe, tests green, quality check passed), so per spec they "se mohou
    mergovat automaticky po zelených testech". Without this, the change
    would exist only on an orphan branch nobody merges: the running app
    (and any successive self-improve iteration) would never see it on disk,
    and a newly created action file would vanish the instant the worktree
    returns to the base branch, defeating "okamžitě zpřístupnit agentovi".
    The self-improve/* branch itself is kept (not deleted) as a named,
    revertible audit trail of exactly what changed and when.

    On regression/failure, the worktree simply returns to the branch it
    started on — nothing is merged, so a bad change never lands anywhere
    reachable from the base branch.
    """
    paths  = _diff_paths(diff)
    module = Path(paths[0]).stem if paths else "change"
    branch = f"self-improve/{module}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Uncommitted edits to TRACKED files (e.g. a human mid-edit in the same
    # checkout while the scheduler runs) could conflict with the branch
    # switch back to `start` at the end. Untracked files (like a
    # freshly-queued pending_changes/*.json from a different iteration of
    # this same run) are harmless — checkout never touches them — so only
    # tracked modifications block the apply.
    dirty = dirty_tracked_files(base_dir=BASE_DIR)
    if dirty:
        print("[SelfMod] Worktree has uncommitted tracked changes — skipping "
              f"apply to avoid bundling unrelated local edits into a "
              f"self-improve commit: {dirty[:3]}")
        return False

    before = run_tests()
    after  = before

    start = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if _git("checkout", "-b", branch).returncode != 0:
        return False

    patch_file = BASE_DIR / "pending_changes" / f".apply-{diff_hash(diff)}.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")

    # Snapshot the pre-change sources for the post-apply quality gate.
    before_sources = {
        p: ((BASE_DIR / p).read_text(encoding="utf-8", errors="replace")
            if (BASE_DIR / p).is_file() else None)
        for p in paths
    }

    ok = False
    try:
        flags = apply_patch_with_fallback(patch_file)
        if flags is None:
            check = _git("apply", "--check", "--whitespace=nowarn", str(patch_file))
            print(f"[SelfMod] Patch does not apply: {check.stderr.strip()[:200]}")
            _git("checkout", start)
            _git("branch", "-D", branch)
            return False

        _git("apply", *flags, str(patch_file))
        _git("add", "--", *paths)
        _git("commit", "-m",
             f"self-improve({module}): {risk_level} change {diff_hash(diff)}\n\n"
             f"Applied by SELF_IMPROVE mode. Rollback: git revert this commit.")

        after = run_tests()
        q_ok, q_reason = _quality_check(paths, before_sources)
        if _is_regression(before, after) or not q_ok:
            reason = "tests regressed" if _is_regression(before, after) else q_reason
            after = before  # reverted — post-state equals pre-state
            _git("revert", "--no-edit", "HEAD")
            print(f"[SelfMod] Change reverted — {reason}.")
        else:
            ok = True
    finally:
        patch_file.unlink(missing_ok=True)
        _git("checkout", start)
        if ok:
            merged = _git("merge", "--ff-only", branch)
            if merged.returncode != 0:
                # Base moved on since branch was cut (e.g. a concurrent apply) —
                # keep the branch for manual/human merge rather than force it.
                ok = False
                print(f"[SelfMod] Applied on {branch} but could not fast-forward "
                      f"{start}: {merged.stderr.strip()[:200]}")
        log_change({
            "module": ", ".join(paths),
            "event": "apply_diff",
            "risk_level": risk_level,
            "applied": ok,
            "approved_by": approved_by,
            "branch": branch,
            "diff_summary": f"hash={diff_hash(diff)}, files={len(paths)}",
            "test_result": {"before_failed": _failed_count(before),
                            "after_failed": _failed_count(after)},
        })
    return ok


# ── Tests ────────────────────────────────────────────────────────────────────

def run_tests(scope: str | None = None) -> dict:
    """Run pytest if available, else import-smoke the project's modules."""
    tests_dir = BASE_DIR / "tests"
    if tests_dir.is_dir():
        target = [str(tests_dir)] if scope is None else ["-k", scope, str(tests_dir)]
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *target],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300,
            )
            return {"runner": "pytest", "returncode": r.returncode,
                    "passed": r.returncode == 0, "output": r.stdout[-3000:]}
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            return {"runner": "pytest", "returncode": -1, "passed": False,
                    "output": "pytest timed out"}
    return _import_smoke(scope)


def _import_smoke(scope: str | None = None) -> dict:
    """Import every project module in a subprocess; missing 3rd-party deps are skipped."""
    modules = []
    for path in list_files("."):
        if path.endswith(".py") and "/" in path and not path.startswith(("tests/", "scripts/")):
            modules.append(path[:-3].replace("/", "."))
    if scope:
        modules = [m for m in modules if scope in m]

    script = (
        "import importlib, json, sys\n"
        "results = {}\n"
        f"for mod in {modules!r}:\n"
        "    try:\n"
        "        importlib.import_module(mod)\n"
        "        results[mod] = 'ok'\n"
        "    except ImportError as e:\n"
        "        results[mod] = 'skipped: ' + str(e)[:80]\n"
        "    except Exception as e:\n"
        "        results[mod] = 'error: ' + str(e)[:120]\n"
        "print(json.dumps(results))\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", script], cwd=str(BASE_DIR),
                           capture_output=True, text=True, timeout=120)
        results = json.loads(r.stdout.strip() or "{}")
    except Exception as e:
        return {"runner": "import_smoke", "passed": False, "output": str(e), "results": {}}

    errors = {m: s for m, s in results.items() if s.startswith("error")}
    return {"runner": "import_smoke", "passed": not errors,
            "results": results, "errors": errors,
            "output": f"{len(results)} modules, {len(errors)} errors"}


def _failed_count(result: dict) -> int:
    if result.get("runner") == "pytest":
        return 0 if result.get("passed") else 1
    return len(result.get("errors", {}))


def _is_regression(before: dict, after: dict) -> bool:
    """A change is a regression if it introduces failures that weren't there."""
    return _failed_count(after) > _failed_count(before)


def _quality_check(paths: list[str], before_sources: dict) -> tuple[bool, str]:
    """Guard the auto-apply SAFE path against silent code degradation.

    Tests alone don't catch a change that is 'safe' and green yet worse — e.g.
    a model that moves a module docstring below the imports, so the module
    loses its ``__doc__``. Each changed .py file must still parse and must not
    drop a module docstring it previously had. Returns ``(ok, reason)``.
    """
    import ast
    for p in paths:
        if not p.endswith(".py"):
            continue
        fp = BASE_DIR / p
        if not fp.is_file():
            continue
        after_src = fp.read_text(encoding="utf-8", errors="replace")
        try:
            after_tree = ast.parse(after_src)
        except SyntaxError as e:
            return False, f"{p} no longer parses ({e.msg})"
        before_src = before_sources.get(p)
        if before_src:
            try:
                had_doc = ast.get_docstring(ast.parse(before_src)) is not None
            except SyntaxError:
                had_doc = False
            if had_doc and ast.get_docstring(after_tree) is None:
                return False, f"{p} lost its module docstring"
    return True, ""


# ── Audit log ────────────────────────────────────────────────────────────────

def log_change(entry: dict) -> None:
    """Append one structured record to the audit log (JSON lines)."""
    entry = {"timestamp": _now(), **entry}
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_log(limit: int = 50) -> list[dict]:
    """Last `limit` audit entries, newest last."""
    try:
        lines = AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
