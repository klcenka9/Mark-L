"""
Central registry for dynamically added actions (skills).

Actions created by SELF_IMPROVE mode live as ordinary modules in actions/
and are listed in actions/registry.json. main.py appends their tool
declarations to TOOL_DECLARATIONS at startup and dispatches unknown tool
names here. Newly registered actions are born YELLOW — they must earn
GREEN through error-free runtime calls (see core.self_mod.record_call).
"""

import importlib
import json
import re
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR      = get_base_dir()
REGISTRY_PATH = BASE_DIR / "actions" / "registry.json"


def load_registry() -> dict:
    """Read registry.json; empty registry if missing/corrupt."""
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"actions": []}


def _save_registry(data: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_plugin_declarations() -> list[dict]:
    """Tool declarations of all registered plugin actions (for the Live config)."""
    return [a["declaration"] for a in load_registry()["actions"] if a.get("declaration")]


def register_action(name: str, module: str, entry: str, declaration: dict) -> str:
    """Register a new action; it starts YELLOW and must earn GREEN."""
    from core import self_mod

    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"Invalid action name: {name}")
    if not module.startswith("actions."):
        raise ValueError("Plugin actions must live in the actions/ package.")

    data = load_registry()
    if any(a["name"] == name for a in data["actions"]):
        raise ValueError(f"Action '{name}' is already registered.")

    data["actions"].append({
        "name": name, "module": module, "entry": entry,
        "declaration": declaration,
    })
    _save_registry(data)

    module_path = module.replace(".", "/") + ".py"
    self_mod.set_status(module_path, "YELLOW", "newly registered action — must earn GREEN")
    self_mod.log_change({
        "module": module_path, "event": "action_registered",
        "risk_level": "safe", "applied": True,
        "diff_summary": f"registered action '{name}' ({module}.{entry})",
    })
    return f"Action '{name}' registered (status: YELLOW)."


def dispatch(name: str, parameters: dict, **kwargs) -> str | None:
    """Run a registered plugin action; None if the name is not registered."""
    from core import self_mod

    entry_rec = next((a for a in load_registry()["actions"] if a["name"] == name), None)
    if entry_rec is None:
        return None

    module_path = entry_rec["module"].replace(".", "/") + ".py"
    try:
        mod = importlib.import_module(entry_rec["module"])
        fn  = getattr(mod, entry_rec["entry"])
        result = fn(parameters=parameters, **kwargs)
        self_mod.record_call(module_path, ok=True)
        return str(result) if result is not None else "Done."
    except Exception as e:
        self_mod.record_call(module_path, ok=False, error=str(e))
        raise
