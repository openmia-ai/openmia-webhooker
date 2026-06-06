from __future__ import annotations

import argparse
import sys

from . import doctor
from .adapters import claude_code, codex
from .banner import OPENMIA_BANNER


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["openmia"]:
        print(f"\n{OPENMIA_BANNER}\n")
        return 0

    parser = argparse.ArgumentParser(prog="openmia-webhooker")
    parser.add_argument("adapter", choices=("codex", "claude-code", "claude-code-run", "claude-code-watch", "doctor"))
    if args and args[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if not args:
        parser.parse_args(args)
        return 0

    adapter, remaining = args[0], args[1:]
    if adapter == "doctor":
        return doctor.main(remaining)
    if adapter == "codex":
        return codex.main(remaining)
    if adapter == "claude-code":
        return claude_code.main(remaining)
    if adapter == "claude-code-run":
        return claude_code.run_main(remaining)
    if adapter == "claude-code-watch":
        return claude_code.watch_main(remaining)
    parser.parse_args([adapter])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
