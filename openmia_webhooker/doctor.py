from __future__ import annotations

import argparse
import json
import platform
import sys
from typing import Any

from .adapters.claude_code import find_claude_command, resolve_claude_projects_dir
from .config import OpenMIAConfig


def collect_diagnostics() -> dict[str, Any]:
    config = OpenMIAConfig.from_env()
    claude_command, claude_command_found = find_claude_command()
    claude_projects_dir = resolve_claude_projects_dir()
    return {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "base_dir": str(config.base_dir),
        "env_path": str(config.env_path),
        "env_exists": config.env_path.exists(),
        "state_dir": str(config.state_dir),
        "log_path": str(config.log_path),
        "ingest_key_configured": bool(config.ingest_key),
        "capture_text": config.capture_text,
        "claude_command": claude_command,
        "claude_command_found": claude_command_found,
        "claude_projects_dir": str(claude_projects_dir),
        "claude_projects_dir_exists": claude_projects_dir.exists(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openmia-webhooker doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    diagnostics = collect_diagnostics()
    if args.json:
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key, value in diagnostics.items():
            print(f"{key}: {value}")
    return 0
