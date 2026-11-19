"""Load WorkSwarm's shipped permissions block, apply the tutorial's edits, build the real engine.

The engine here is the same one WorkSwarm runs: WorkSwarm composes the Global,
User and Session layers with ``compose_host_effective_permissions`` and hands
the result to openJiuwen Agent Core's ``PermissionEngine``.
"""
from __future__ import annotations

import contextlib
import copy
import io
import logging
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
EDITS_FILE = REPO / "config" / "permissions-edits.yaml"


def shipped_permissions() -> dict[str, Any]:
    """The permissions block jiuwenswarm-init copies into config.yaml."""
    text = resources.files("jiuwenswarm").joinpath("resources/config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["permissions"]


def tutorial_edits() -> dict[str, Any]:
    return yaml.safe_load(EDITS_FILE.read_text(encoding="utf-8"))["permissions"]


def apply_edits(perms: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """Scalars and mapping keys replace; ``rules`` and ``file_guard.paths`` append."""
    out = copy.deepcopy(perms)
    for key, value in edits.items():
        if key == "rules":
            out["rules"] = list(out.get("rules") or []) + copy.deepcopy(value)
        elif key == "file_guard":
            fg = out.setdefault("file_guard", {})
            for fkey, fval in value.items():
                if fkey == "paths":
                    fg["paths"] = list(fg.get("paths") or []) + copy.deepcopy(fval)
                elif isinstance(fval, dict):
                    fg.setdefault(fkey, {}).update(fval)
                else:
                    fg[fkey] = fval
        elif isinstance(value, dict):
            out.setdefault(key, {}).update(value)
        else:
            out[key] = value
    return out


def apply_edits_to_config_file(config_path: Path) -> None:
    """Apply the edits to a real config.yaml, keeping its comments and layout."""
    from ruamel.yaml import YAML

    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 4096
    data = ry.load(config_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    for key, value in tutorial_edits().items():
        if key == "rules":
            perms["rules"].extend(value)
        elif key == "file_guard":
            for fkey, fval in value.items():
                if fkey == "paths":
                    perms["file_guard"]["paths"].extend(fval)
                elif isinstance(fval, dict):
                    perms["file_guard"][fkey].update(fval)
                else:
                    perms["file_guard"][fkey] = fval
        elif isinstance(value, dict):
            perms[key].update(value)
        else:
            perms[key] = value
    with config_path.open("w", encoding="utf-8") as f:
        ry.dump(data, f)


def build_engine(perms: dict[str, Any], workspace: Path):
    """WorkSwarm's compose step plus Agent Core's PermissionEngine, as the runtime wires them."""
    # Agent Core logs its start-up INFO lines to stdout; keep the check output readable.
    logging.disable(logging.INFO)
    with contextlib.redirect_stdout(io.StringIO()):
        from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
            compose_host_effective_permissions,
        )
        from openjiuwen.harness.security.permission_engine.core import PermissionEngine

    effective = compose_host_effective_permissions(global_permissions=perms)
    return PermissionEngine(config=effective, workspace_root=workspace)
