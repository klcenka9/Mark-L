"""
Safety-gate tests for core/self_mod.py.

This file is itself in PROTECTED_PATHS: the agent cannot weaken these
tests through its own pipeline. They verify the meta-rule — protected
paths always classify as core_safety_change and apply_diff() refuses them.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import self_mod  # noqa: E402


def _diff_for(path: str, added_line: str = "x = 1") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        " existing\n"
        f"+{added_line}\n"
    )


# ── classify_change ──────────────────────────────────────────────────────────

def test_protected_paths_are_core_safety():
    for path in self_mod.PROTECTED_PATHS:
        assert self_mod.classify_change(_diff_for(path)) == "core_safety_change", path


def test_protected_paths_cover_the_orchestrator_surface():
    """Regression test: a 'safe' diff bumping the loop's own run limits, or
    weakening registry validation, or touching the tool-dispatch wiring in
    main.py, must all be core_safety_change — not just core/self_mod.py."""
    for path in ("actions/self_improve.py", "actions/registry.py",
                 "main.py", "tests/test_smoke_imports.py"):
        assert path in self_mod.PROTECTED_PATHS, path
        assert self_mod.classify_change(_diff_for(path)) == "core_safety_change", path


def test_self_mod_is_protected_even_with_harmless_content():
    diff = _diff_for("core/self_mod.py", "# tiny comment improvement")
    assert self_mod.classify_change(diff) == "core_safety_change"


def test_dangerous_paths():
    assert self_mod.classify_change(_diff_for("actions/computer_settings.py")) == "dangerous"
    assert self_mod.classify_change(_diff_for("requirements.txt")) == "dangerous"
    assert self_mod.classify_change(_diff_for("config/settings.json")) == "dangerous"


def test_dangerous_content_in_safe_path():
    assert self_mod.classify_change(
        _diff_for("actions/web_search.py", "shutil.rmtree(user_dir)")) == "dangerous"
    assert self_mod.classify_change(
        _diff_for("actions/web_search.py", 'password = "hunter2"')) == "dangerous"


def test_safe_change():
    diff = _diff_for("actions/web_search.py", "    # clearer log line")
    assert self_mod.classify_change(diff) == "safe"


def test_mixed_diff_takes_highest_risk():
    diff = _diff_for("actions/web_search.py") + _diff_for("core/self_mod.py")
    assert self_mod.classify_change(diff) == "core_safety_change"


def test_empty_diff_rejected():
    with pytest.raises(ValueError):
        self_mod.classify_change("not a diff at all")


# ── apply_diff gate ──────────────────────────────────────────────────────────

def test_apply_refuses_dirty_worktree(tmp_path, monkeypatch):
    """An uncommitted edit to a tracked file (e.g. a human mid-edit while
    the scheduler runs) must block the apply rather than risk being swept
    into the self-improve commit — untracked files are fine and ignored."""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    (repo / "actions" / "x.py").write_text("a = 1\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(self_mod, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", repo / "logs" / "audit.jsonl")
    monkeypatch.setattr(self_mod, "STATUS_PATH", repo / "status.json")
    monkeypatch.setattr(self_mod, "run_tests", lambda scope=None: {"passed": True, "runner": "stub"})

    diff = _diff_for("actions/y.py", "y = 1")
    # Harmless untracked side-effect file — must NOT block the apply.
    (repo / "pending_changes").mkdir(exist_ok=True)
    (repo / "pending_changes" / "unrelated.json").write_text("{}")
    assert self_mod.apply_diff(diff, "safe") is False  # y.py doesn't exist yet as context

    # A genuine uncommitted edit to a TRACKED file — must block.
    (repo / "actions" / "x.py").write_text("a = 2  # uncommitted human edit\n", encoding="utf-8")
    diff2 = self_mod.make_create_file_diff("actions/z.py", "z = 1\n")
    assert self_mod.dirty_tracked_files(base_dir=repo)
    assert self_mod.apply_diff(diff2, "safe") is False
    assert not (repo / "actions" / "z.py").exists()
    # The human's edit survives untouched.
    assert "uncommitted human edit" in (repo / "actions" / "x.py").read_text()


def test_apply_diff_refuses_core_safety(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    with pytest.raises(PermissionError):
        self_mod.apply_diff(_diff_for("core/self_mod.py"), "safe")  # mislabel ignored


def test_apply_diff_refuses_even_when_caller_lies(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    with pytest.raises(PermissionError):
        self_mod.apply_diff(_diff_for("core/prompt.txt"), "safe")


def test_dangerous_without_approval_is_queued_not_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    diff = _diff_for("actions/computer_settings.py")
    assert self_mod.apply_diff(diff, "dangerous") is False
    pending = self_mod.list_pending()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["diff_hash"] == self_mod.diff_hash(diff)


def test_approval_must_match_diff_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    diff = _diff_for("actions/computer_settings.py", "volume = 10")
    self_mod.apply_diff(diff, "dangerous")            # queued
    rec = self_mod.list_pending()[0]
    rec["status"], rec["approved_by"] = "approved", "human"
    (self_mod.PENDING_DIR / f"{rec['id']}.json").write_text(json.dumps(rec))
    # A DIFFERENT dangerous diff must not ride on that approval
    other = _diff_for("actions/computer_settings.py", "volume = 99")
    assert self_mod.apply_diff(other, "dangerous") is False


# ── status system ────────────────────────────────────────────────────────────

def test_status_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    mod = "actions/fake.py"
    for _ in range(10):
        self_mod.record_call(mod, ok=True)
    assert self_mod._load_status()[mod]["status"] == "GREEN"
    for _ in range(5):
        self_mod.record_call(mod, ok=False, error="boom")
    assert self_mod._load_status()[mod]["status"] == "RED"


def test_status_change_is_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    self_mod.set_status("actions/fake.py", "YELLOW", "new module")
    events = self_mod.read_audit_log()
    assert any(e["event"] == "status_change" for e in events)


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        self_mod.set_status("actions/fake.py", "PURPLE", "nope")


# ── audit log ────────────────────────────────────────────────────────────────

def test_log_change_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    self_mod.log_change({"module": "x", "event": "test"})
    self_mod.log_change({"module": "y", "event": "test"})
    entries = self_mod.read_audit_log()
    assert len(entries) == 2
    assert all("timestamp" in e for e in entries)


# ── path safety ──────────────────────────────────────────────────────────────

def test_read_file_refuses_escape():
    with pytest.raises(ValueError):
        self_mod.read_file("../../etc/passwd")


# ── proposal parsing (robust to real model output) ───────────────────────────

def test_parse_proposal_fenced_blocks_with_quotes():
    """A diff full of quotes survives because it lives in its own fence,
    not inside a JSON string (the bug a real Gemini run exposed)."""
    text = (
        "Here you go:\n"
        "```json\n"
        '{"rationale": "add docstring", "expected_effect": "clearer",'
        ' "risk_level": "safe", "rollback_plan": "git revert", "tests_to_run": []}\n'
        "```\n"
        "```diff\n"
        "--- a/actions/x.py\n"
        "+++ b/actions/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        ' import os\n'
        '+x = """triple quoted "" value"""\n'
        "```\n"
    )
    p = self_mod._parse_proposal(text)
    assert p["rationale"] == "add docstring"
    assert '"""triple quoted' in p["diff"]        # quotes preserved verbatim
    assert p["diff"].startswith("--- a/actions/x.py")


def test_parse_proposal_json_fallback():
    """Older single-JSON-object answers with an embedded diff still parse."""
    text = json.dumps({
        "diff": "--- a/actions/x.py\n+++ b/actions/x.py\n@@ -1 +1,2 @@\n a\n+b\n",
        "rationale": "r", "expected_effect": "e",
        "risk_level": "safe", "rollback_plan": "revert",
    })
    p = self_mod._parse_proposal(text)
    assert "actions/x.py" in p["diff"]
    assert p["rationale"] == "r"


def test_parse_proposal_no_diff_raises():
    with pytest.raises(ValueError):
        self_mod._parse_proposal("```json\n{\"rationale\": \"x\"}\n```")


def _git_out(repo, *args) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def test_apply_tolerates_wrong_hunk_counts_and_crlf(tmp_path, monkeypatch):
    """A model diff with wrong @@ counts against a CRLF file still applies
    (via the --recount / --ignore-whitespace fallback strategies)."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "actions").mkdir()
    # CRLF file, like this repo's sources.
    (repo / "actions" / "x.py").write_bytes(b"import os\r\nprint(1)\r\n")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(self_mod, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", repo / "logs" / "audit.jsonl")
    monkeypatch.setattr(self_mod, "STATUS_PATH", repo / "status.json")
    monkeypatch.setattr(self_mod, "run_tests", lambda scope=None: {"passed": True, "runner": "stub"})

    start_branch = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    # LF diff, deliberately wrong count (@@ -1,1 should be -1,2), targeting CRLF file.
    diff = (
        "--- a/actions/x.py\n"
        "+++ b/actions/x.py\n"
        "@@ -1,1 +1,3 @@\n"
        " import os\n"
        "+import sys\n"
        " print(1)\n"
    )
    assert self_mod.apply_diff(diff, "safe") is True

    # The change landed on its own self-improve/* branch (kept as a named,
    # revertible audit trail)...
    branches = _git_out(repo, "branch", "--list", "self-improve/*").strip()
    assert branches, "expected a self-improve/* branch to be created"
    new_branch = branches.lstrip("* ").strip()
    assert "import sys" in _git_out(repo, "show", f"{new_branch}:actions/x.py")

    # ...AND was fast-forward-merged back into the branch we started from —
    # per spec, SAFE changes "se mohou mergovat automaticky po zelených
    # testech" — so the running app and the next self-improve iteration
    # actually see the change, instead of it being marooned on an orphan
    # branch nobody merges.
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == start_branch
    assert b"import sys" in (repo / "actions" / "x.py").read_bytes()


def test_safe_change_degrading_quality_is_auto_reverted(tmp_path, monkeypatch):
    """A 'safe', tests-green change that silently degrades code (module loses
    its docstring) is caught by the quality gate and reverted."""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    (repo / "actions" / "y.py").write_text('"""Module doc."""\nimport os\n', encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(self_mod, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", repo / "logs" / "audit.jsonl")
    monkeypatch.setattr(self_mod, "STATUS_PATH", repo / "status.json")
    monkeypatch.setattr(self_mod, "run_tests", lambda scope=None: {"passed": True, "runner": "stub"})

    # Moves the module docstring below the import → module loses __doc__.
    diff = (
        "--- a/actions/y.py\n"
        "+++ b/actions/y.py\n"
        "@@ -1,2 +1,2 @@\n"
        '-"""Module doc."""\n'
        " import os\n"
        '+"""Module doc."""\n'
    )
    assert self_mod.apply_diff(diff, "safe") is False           # reverted
    assert (repo / "actions" / "y.py").read_text().startswith('"""Module doc."""')


# ── review gate (shared backend of CLI and UI dialogs) ───────────────────────

import subprocess


def _scratch_repo(tmp_path, monkeypatch, files: dict[str, str]):
    """Init a throwaway git repo and point self_mod's paths at it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(self_mod, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", repo / "logs" / "audit.jsonl")
    monkeypatch.setattr(self_mod, "STATUS_PATH", repo / "status.json")
    return repo


def test_review_gate_refuses_core_safety_via_dangerous_flow(tmp_path, monkeypatch):
    from core import review_gate
    _scratch_repo(tmp_path, monkeypatch, {"core/prompt.txt": "persona\n"})
    pid = self_mod.queue_pending_change({
        "diff": _diff_for("core/prompt.txt"), "risk_level": "core_safety_change",
        "rationale": "sneaky prompt edit",
    })
    with pytest.raises(PermissionError):
        review_gate.approve_dangerous(pid, "human")


def test_core_safety_approval_resets_to_pending_on_apply_failure(tmp_path, monkeypatch):
    """A core-safety approval whose patch fails to apply (e.g. base moved on)
    must not get stuck at status='approved' forever — it resets to
    'pending' so it still shows up in the review UI/CLI for a retry,
    instead of silently vanishing from the queue."""
    from core import review_gate
    _scratch_repo(tmp_path, monkeypatch, {"core/prompt.txt": "persona\n"})
    # A diff that can never apply (context line doesn't exist in the file).
    bad_diff = (
        "diff --git a/core/prompt.txt b/core/prompt.txt\n"
        "--- a/core/prompt.txt\n"
        "+++ b/core/prompt.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " this context line does not exist\n"
        "+new rule\n"
    )
    pid = self_mod.queue_pending_change(
        {"diff": bad_diff, "risk_level": "core_safety_change", "rationale": "x"})
    msg = review_gate.approve_and_apply_core_safety(pid, "tester")
    assert "reset to pending" in msg.lower()
    rec = review_gate.get_pending(pid)
    assert rec["status"] == "pending"
    assert rec["approved_by"] is None
    # Shows up again for review.
    assert any(r["status"] == "pending" for r in self_mod.list_pending())


def test_review_gate_approve_dangerous_applies_on_branch(tmp_path, monkeypatch):
    from core import review_gate
    repo = _scratch_repo(tmp_path, monkeypatch,
                         {"actions/computer_settings.py": "volume = 1\n"})
    diff = (
        "diff --git a/actions/computer_settings.py b/actions/computer_settings.py\n"
        "--- a/actions/computer_settings.py\n"
        "+++ b/actions/computer_settings.py\n"
        "@@ -1,1 +1,2 @@\n"
        " volume = 1\n"
        "+volume_max = 100\n"
    )
    start_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=repo, capture_output=True, text=True).stdout.strip()
    pid = self_mod.queue_pending_change(
        {"diff": diff, "risk_level": "dangerous", "rationale": "add max"})
    msg = review_gate.approve_dangerous(pid, "tester")
    assert "applied" in msg.lower()
    rec = review_gate.get_pending(pid)
    assert rec["status"] == "applied" and rec["approved_by"] == "tester"
    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    assert "self-improve/" in branches
    new_branch = next(b.lstrip("* ").strip() for b in branches.splitlines()
                      if "self-improve/" in b)
    shown = subprocess.run(["git", "show", f"{new_branch}:actions/computer_settings.py"],
                           cwd=repo, capture_output=True, text=True).stdout
    assert "volume_max" in shown
    # Worktree is back on the branch it started from (not left on the new
    # one), and that branch was fast-forwarded to include the change — a
    # DANGEROUS change applies via the same _apply_on_branch as SAFE, once
    # a human has approved it.
    end_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=repo, capture_output=True, text=True).stdout.strip()
    assert end_branch == start_branch
    assert "volume_max" in (repo / "actions/computer_settings.py").read_text()


def test_review_gate_core_safety_applies_on_dedicated_branch(tmp_path, monkeypatch):
    from core import review_gate
    repo = _scratch_repo(tmp_path, monkeypatch, {"core/prompt.txt": "persona\n"})
    diff = (
        "diff --git a/core/prompt.txt b/core/prompt.txt\n"
        "--- a/core/prompt.txt\n"
        "+++ b/core/prompt.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " persona\n"
        "+new rule\n"
    )
    start_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=repo, capture_output=True, text=True).stdout.strip()
    pid = self_mod.queue_pending_change(
        {"diff": diff, "risk_level": "core_safety_change", "rationale": "prompt rule"})
    msg = review_gate.approve_and_apply_core_safety(pid, "tester")
    assert "dedicated core-safety branch" in msg
    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    assert "core-safety/" in branches
    new_branch = next(b.lstrip("* ").strip() for b in branches.splitlines()
                      if "core-safety/" in b)
    shown_bytes = subprocess.run(["git", "show", f"{new_branch}:core/prompt.txt"],
                                 cwd=repo, capture_output=True).stdout
    assert b"new rule" in shown_bytes
    # Worktree returns to the branch it started from, unmodified.
    end_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=repo, capture_output=True, text=True).stdout.strip()
    assert end_branch == start_branch
    assert "new rule" not in (repo / "core/prompt.txt").read_text()

    rec = review_gate.get_pending(pid)
    assert rec["status"] == "applied"
    # applied_content_hash must match the file's content ON THE NEW BRANCH
    # (where the change actually landed), not on the worktree's current branch.
    import hashlib
    expected = hashlib.sha256(
        b"core/prompt.txt\0" + shown_bytes + b"\0").hexdigest()[:16]
    assert rec["applied_content_hash"] == expected

    events = self_mod.read_audit_log()
    approval = [e for e in events if e.get("event") == "approval"]
    assert approval and approval[-1]["approved_by"] == "tester"
    assert "hash=" in approval[-1]["diff_summary"]


def test_ci_gate_bootstrap_vs_enforcement(tmp_path, monkeypatch):
    """The CI gate passes only while the base ref lacks the gate itself."""
    import importlib.util
    from pathlib import Path as _P
    gate_src = _P(__file__).resolve().parent.parent / "scripts" / "check_protected_paths.py"
    spec = importlib.util.spec_from_file_location("gate_under_test", gate_src)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    repo = _scratch_repo(tmp_path, monkeypatch, {"core/prompt.txt": "persona\n"})
    monkeypatch.setattr(gate, "BASE_DIR", repo)

    # Bootstrap: HEAD has no gate script -> staged protected change passes
    (repo / "core/prompt.txt").write_text("persona\nedited\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/prompt.txt"], cwd=repo, check=True)
    monkeypatch.setattr("sys.argv", ["check_protected_paths.py"])
    assert gate.main() == 0

    # Enforcement: commit the gate into HEAD -> same staged change is refused
    subprocess.run(["git", "commit", "-qm", "edit"], cwd=repo, check=True)
    gate_copy = repo / "scripts" / "check_protected_paths.py"
    gate_copy.parent.mkdir(parents=True, exist_ok=True)
    gate_copy.write_text(gate_src.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add gate"], cwd=repo, check=True)
    (repo / "core/prompt.txt").write_text("persona\nedited again\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/prompt.txt"], cwd=repo, check=True)
    assert gate.main() == 1


def test_ci_gate_stale_approval_does_not_cover_a_different_change(tmp_path, monkeypatch):
    """CRITICAL regression test.

    A previously-merged, genuinely-approved core-safety record must NEVER
    let a later, different, unapproved change to the same protected file
    through the gate. Before the fix, `_has_core_safety_approval()` only
    checked "does ANY approved core_safety_change record exist anywhere in
    pending_changes/" — so once merged, that one record permanently
    defeated the gate for every future PR. This test reproduces exactly
    that scenario and asserts it is now refused.
    """
    import importlib.util
    from pathlib import Path as _P
    gate_src = _P(__file__).resolve().parent.parent / "scripts" / "check_protected_paths.py"
    spec = importlib.util.spec_from_file_location("gate_under_test2", gate_src)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    repo = _scratch_repo(tmp_path, monkeypatch, {"core/prompt.txt": "persona\n"})
    monkeypatch.setattr(gate, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")

    # Bring the gate itself into HEAD so we're testing "enforcement", not bootstrap.
    gate_copy = repo / "scripts" / "check_protected_paths.py"
    gate_copy.parent.mkdir(parents=True, exist_ok=True)
    gate_copy.write_text(gate_src.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add gate"], cwd=repo, check=True)

    # A GENUINE, correctly-hash-bound approval for an OLD, already-applied change:
    # compute the hash of exactly the content that change would leave on disk.
    old_content = "persona\nold approved rule\n"
    (repo / "core/prompt.txt").write_text(old_content, encoding="utf-8")
    old_hash = self_mod.protected_content_hash(["core/prompt.txt"], base_dir=repo)

    (repo / "pending_changes").mkdir(exist_ok=True)
    stale_rec = {
        "id": "old-approval", "risk_level": "core_safety_change",
        "status": "applied", "approved_by": "human",
        "applied_content_hash": old_hash,
    }
    (repo / "pending_changes" / "old-approval.json").write_text(json.dumps(stale_rec))

    monkeypatch.setattr("sys.argv", ["check_protected_paths.py"])

    # Sanity: the stale record DOES cover the matching (old_content) state.
    subprocess.run(["git", "add", "core/prompt.txt"], cwd=repo, check=True)
    assert gate.main() == 0, "a hash-matching approval must still pass"

    # Commit that approved change so it's no longer staged.
    subprocess.run(["git", "commit", "-qm", "apply approved change"], cwd=repo, check=True)

    # Now a DIFFERENT, NEVER-APPROVED change touches the same protected file.
    # The stale record (for the OLD content) must NOT cover this new content.
    (repo / "core/prompt.txt").write_text("persona\nold approved rule\nBACKDOOR\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/prompt.txt"], cwd=repo, check=True)
    assert gate.main() == 1, (
        "a stale approval for different content must NOT let a new, "
        "unapproved change to the same protected file through"
    )


def test_protected_content_hash_matches_between_self_mod_and_ci_gate(tmp_path):
    """The two independent hash implementations (core/self_mod.py's real
    one, and the literal copy in scripts/check_protected_paths.py that must
    keep working even if self_mod.py is compromised) must agree exactly."""
    import importlib.util
    gate_src = Path(__file__).resolve().parent.parent / "scripts" / "check_protected_paths.py"
    spec = importlib.util.spec_from_file_location("gate_under_test3", gate_src)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "prompt.txt").write_text("persona\nrule\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "api_keys.json").write_text('{"k": "v"}', encoding="utf-8")

    paths = ["core/prompt.txt", "config/api_keys.json", "core/does_not_exist.py"]
    monkeypatch_base = tmp_path
    gate.BASE_DIR = monkeypatch_base
    a = self_mod.protected_content_hash(paths, base_dir=monkeypatch_base)
    b = gate._protected_content_hash(paths)
    assert a == b


def test_make_create_file_diff_is_appliable(tmp_path, monkeypatch):
    """A brand-new action file can be created through the normal SAFE apply
    pipeline (git branch, tests, commit) via a generated create-file diff."""
    repo = _scratch_repo(tmp_path, monkeypatch, {"actions/__init__.py": ""})
    monkeypatch.setattr(self_mod, "run_tests", lambda scope=None: {"passed": True, "runner": "stub"})

    code = 'def run(parameters, **kwargs):\n    return "ok"\n'
    diff = self_mod.make_create_file_diff("actions/demo_new_skill.py", code)
    assert self_mod.classify_change(diff) == "safe"
    assert self_mod.apply_diff(diff, "safe") is True

    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    new_branch = next(b.lstrip("* ").strip() for b in branches.splitlines()
                      if "self-improve/" in b)
    shown = subprocess.run(["git", "show", f"{new_branch}:actions/demo_new_skill.py"],
                           cwd=repo, capture_output=True, text=True).stdout
    assert "def run(parameters" in shown


def test_review_gate_reject_is_logged(tmp_path, monkeypatch):
    from core import review_gate
    _scratch_repo(tmp_path, monkeypatch, {"actions/x.py": "x = 1\n"})
    pid = self_mod.queue_pending_change({
        "diff": _diff_for("actions/computer_settings.py"),
        "risk_level": "dangerous", "rationale": "meh",
    })
    review_gate.reject_change(pid, "not needed", "tester")
    rec = review_gate.get_pending(pid)
    assert rec["status"] == "rejected" and rec["rejected_reason"] == "not needed"
    assert any(e["event"] == "rejection" for e in self_mod.read_audit_log())
