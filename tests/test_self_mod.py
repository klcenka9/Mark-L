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
    pid = self_mod.queue_pending_change(
        {"diff": diff, "risk_level": "dangerous", "rationale": "add max"})
    msg = review_gate.approve_dangerous(pid, "tester")
    assert "applied" in msg.lower()
    assert "volume_max" in (repo / "actions/computer_settings.py").read_text()
    rec = review_gate.get_pending(pid)
    assert rec["status"] == "applied" and rec["approved_by"] == "tester"
    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    assert "self-improve/" in branches


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
    pid = self_mod.queue_pending_change(
        {"diff": diff, "risk_level": "core_safety_change", "rationale": "prompt rule"})
    msg = review_gate.approve_and_apply_core_safety(pid, "tester")
    assert "dedicated core-safety branch" in msg
    assert "new rule" in (repo / "core/prompt.txt").read_text()
    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    assert "core-safety/" in branches
    events = self_mod.read_audit_log()
    approval = [e for e in events if e.get("event") == "approval"]
    assert approval and approval[-1]["approved_by"] == "tester"
    assert "hash=" in approval[-1]["diff_summary"]


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
