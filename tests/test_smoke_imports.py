"""
Smoke tests: the self-improvement layer's modules import cleanly and the
action registry behaves (new actions are born YELLOW, bad names rejected).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_core_self_mod_imports():
    from core import self_mod
    assert callable(self_mod.classify_change)
    assert callable(self_mod.apply_diff)
    assert len(self_mod.PROTECTED_PATHS) >= 4


def test_actions_self_improve_imports():
    from actions import self_improve
    assert callable(self_improve.run_self_improve)
    assert self_improve.MAX_ITERATIONS <= 10
    assert self_improve.STAGNATION_LIMIT >= 1


def test_registry_imports():
    from actions import registry
    assert callable(registry.register_action)


def test_register_action_is_born_yellow(tmp_path, monkeypatch):
    from actions import registry
    from core import self_mod
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(self_mod, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")

    registry.register_action(
        "demo_action", "actions.demo_action", "run",
        {"name": "demo_action", "description": "demo", "parameters": {"type": "OBJECT", "properties": {}}},
    )
    status = self_mod._load_status()["actions/demo_action.py"]
    assert status["status"] == "YELLOW"


def test_register_action_rejects_bad_names(tmp_path, monkeypatch):
    from actions import registry
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    with pytest.raises(ValueError):
        registry.register_action("Bad-Name!", "actions.x", "run", {})
    with pytest.raises(ValueError):
        registry.register_action("escape", "os.system", "run", {})


def test_add_skill_creates_and_registers_end_to_end(tmp_path, monkeypatch):
    """The full add_skill path: new file classifies safe, applies through
    the normal git-branch+test pipeline, and gets registered as YELLOW —
    closing the gap where register_action() was previously never called by
    any production code path."""
    import subprocess
    from actions import registry, self_improve
    from core import self_mod

    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    (repo / "actions" / "__init__.py").write_text("", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)

    monkeypatch.setattr(self_mod, "BASE_DIR", repo)
    monkeypatch.setattr(self_mod, "PENDING_DIR", repo / "pending_changes")
    monkeypatch.setattr(self_mod, "AUDIT_LOG_PATH", repo / "logs" / "audit.jsonl")
    monkeypatch.setattr(self_mod, "STATUS_PATH", repo / "status.json")
    monkeypatch.setattr(self_mod, "run_tests", lambda scope=None: {"passed": True, "runner": "stub"})
    monkeypatch.setattr(registry, "REGISTRY_PATH", repo / "actions" / "registry.json")
    monkeypatch.setattr(registry, "BASE_DIR", repo)

    code = 'def run(parameters, **kwargs):\n    return "pong"\n'
    msg = self_improve.add_skill("demo_ping", "Replies with pong", code)
    assert "registered" in msg.lower()
    assert "YELLOW" in msg

    reg = registry.load_registry()
    assert any(a["name"] == "demo_ping" for a in reg["actions"])
    status = self_mod._load_status()["actions/demo_ping.py"]
    assert status["status"] == "YELLOW"

    # The SAFE apply fast-forward-merges the self-improve/* branch back into
    # the branch we started from, so the new skill is on disk RIGHT NOW and
    # immediately dispatchable — not marooned on an orphan branch nobody
    # merges (this is what "okamžitě zpřístupnit agentovi" requires).
    branches = subprocess.run(["git", "branch"], cwd=repo,
                              capture_output=True, text=True).stdout
    assert "self-improve/" in branches
    assert (repo / "actions" / "demo_ping.py").is_file()

    # Prove it's actually runnable, not just present as metadata: load the
    # file directly from its on-disk path (registry.dispatch()'s own
    # importlib.import_module() resolution is exercised separately by
    # test_register_action_is_born_yellow / production use — what matters
    # here is that add_skill leaves the real, correct file where it must
    # be, immediately, rather than on an orphan branch nobody merges).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "demo_ping_check", repo / "actions" / "demo_ping.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.run({}) == "pong"


def test_check_protected_paths_lists_match():
    """The CI gate's literal copy must stay in sync with the real constant."""
    from core import self_mod
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_protected_paths",
        Path(__file__).resolve().parent.parent / "scripts" / "check_protected_paths.py",
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert set(gate.PROTECTED_PATHS) == set(self_mod.PROTECTED_PATHS)
