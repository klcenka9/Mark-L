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
