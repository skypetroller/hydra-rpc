#!/usr/bin/env python3
"""Bundle src/*.py into the single-file ./hydra-rpc artifact.

The modules are concatenated in dependency order; statement order is preserved,
so the bundle is semantically identical to the sources. Use --check in CI to
fail when the committed artifact drifts from src/.

Usage:
    python3 build.py            # regenerate ./hydra-rpc
    python3 build.py --check    # fail if ./hydra-rpc differs (modulo blank lines)
"""

import ast
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MODULES = [
    "header.py",
    "ed25519.py",
    "config.py",
    "db.py",
    "updater.py",
    "ipc.py",
    "detection.py",
    "status.py",
    "proc.py",
    "main.py",
]
ARTIFACT = ROOT / "hydra-rpc"


def bundle_text():
    parts = []
    for module in MODULES:
        text = (ROOT / "src" / module).read_text()
        lines = text.splitlines()
        # Drop the per-module header comment; keep everything else verbatim so
        # statement order (and therefore the AST) is unchanged.
        if lines and lines[0].startswith("# hydra-rpc source module:"):
            lines = lines[1:]
        parts.append("\n".join(lines).strip("\n"))
    return "\n\n\n".join(parts) + "\n"


def ast_normalised(text):
    return ast.dump(ast.parse(text))


def main(argv):
    check_only = "--check" in argv
    bundle = bundle_text()
    if check_only:
        current = ARTIFACT.read_text()
        if ast_normalised(bundle) != ast_normalised(current):
            print("build.py --check: ./hydra-rpc drifts from src/ (AST mismatch)", file=sys.stderr)
            print("run: python3 build.py", file=sys.stderr)
            return 1
        print("build.py --check: ./hydra-rpc matches src/ (AST-identical)")
        return 0
    ARTIFACT.write_text(bundle)
    mode = os.stat(ARTIFACT).st_mode
    os.chmod(ARTIFACT, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"wrote {ARTIFACT} ({len(bundle)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
