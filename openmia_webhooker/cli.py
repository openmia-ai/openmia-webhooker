from __future__ import annotations

import argparse
import sys

from .adapters import claude_code, codex


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="openmia-webhooker")
    parser.add_argument("adapter", choices=("codex", "claude-code"))
    if not args or args[0] in {"-h", "--help"}:
        parser.parse_args(args)
        return 0

    adapter, remaining = args[0], args[1:]
    if adapter == "codex":
        return codex.main(remaining)
    if adapter == "claude-code":
        return claude_code.main(remaining)
    parser.parse_args([adapter])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
